from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.capture_v2.c_bridge import CaptureV2CBridge
from app.capture_v2.db_models import CaptureGap, CaptureLease, CaptureSegment
from app.capture_v2.gate.evidence import GateEvidenceCollector
from app.capture_v2.gate.models import GateCaseResult, GateCheck, GateRunPaths, GateVerdict
from app.capture_v2.runtime import capture_authority_mode
from app.core.config import settings


def _verdict(checks: tuple[GateCheck, ...]) -> GateVerdict:
    if any(c.passed is False for c in checks):
        return GateVerdict.FAIL
    if any(c.passed is None for c in checks):
        return GateVerdict.INCONCLUSIVE
    return GateVerdict.PASS


async def run_r7_validation_rollback(
    runner,
    *,
    reproduction_session_id: str,
    device: Any,
    worker_id: str,
    gate_id: str,
    transport: str,
    duration_seconds: float,
) -> GateCaseResult:
    """Real-DUT validation-scope V2 authority start -> cleanup -> V1 restoration.

    This is deliberately not a production cutover. The server must remain configured
    for V1 with Production V2 disabled before and after the rehearsal. The gate starts
    the real V2 fenced producer/lease/segment pipeline directly on one DUT, proves it
    is live and durable, then tears it down and proves exact producer/lease cleanup.
    """
    duration_seconds = max(30.0, min(float(duration_seconds), 180.0))
    pre_mode = capture_authority_mode()
    pre_version = str(settings.capture_engine_version)
    pre_prod_enabled = bool(settings.capture_v2_production_enabled)

    session = await CaptureV2CBridge(
        session_factory=runner.session_factory,
        adapter=runner.adapter,
        profile_root=runner.profile_root,
        requested_profile_id=runner.requested_profile_id,
        transport=transport,
    ).establish(
        reproduction_session_id=reproduction_session_id,
        device=device,
        worker_id=worker_id,
    )
    bootstrap = session.bootstrap
    paths = GateRunPaths.create(runner.output_root, gate_id, str(device.id))

    runtime_errors: list[str] = []
    cleanup_errors: list[str] = []
    active_owned_count = 0
    active_exact_count = 0
    active_lease = False
    active_segment_count = 0
    active_durable_count = 0
    producer_gone = False
    lease_released = False

    try:
        cycles = await session.drain_for(
            duration_seconds=duration_seconds,
            cycle_interval_seconds=0.5,
        )
        session.components["lease"].validate(session.token)
        owned = await session.components["producer"].inspect_owned()
        active_owned_count = len(owned)
        active_exact_count = sum(
            1 for p in owned
            if int(p.pid) == int(bootstrap.ownership.producer.pid)
            and int(p.process_starttime) == int(bootstrap.ownership.producer.process_starttime)
        )
        with runner.session_factory() as db:
            lease = db.get(CaptureLease, str(device.id))
            active_lease = bool(
                lease is not None
                and str(lease.state) == "ACTIVE"
                and str(lease.capture_session_id) == str(bootstrap.capture_session_id)
            )
            segments = list(db.scalars(select(CaptureSegment).where(
                CaptureSegment.capture_session_id == bootstrap.capture_session_id
            )))
        active_segment_count = len(segments)
        durable_states = {"PERSISTED", "ACK_PENDING", "ACKED", "REMOTE_DELETED"}
        active_durable_count = sum(1 for row in segments if row.state in durable_states)
        if not cycles:
            runtime_errors.append("NO_DRAIN_CYCLES")
    except Exception as exc:
        runtime_errors.append(f"{type(exc).__name__}:{exc}")
    finally:
        try:
            await session.stop_lease_renewer(release_lease=False)
        except Exception as exc:
            cleanup_errors.append(f"STOP_RENEWER:{type(exc).__name__}:{exc}")
        try:
            await session.components["producer"].stop_identity(session.token, bootstrap.ownership.producer)
            producer_gone = not await session.components["reader"].process_matches(
                pid=bootstrap.ownership.producer.pid,
                starttime=bootstrap.ownership.producer.process_starttime,
            )
        except Exception as exc:
            cleanup_errors.append(f"STOP_PRODUCER:{type(exc).__name__}:{exc}")
        try:
            session.components["lease"].release(session.token)
            with runner.session_factory() as db:
                lease = db.get(CaptureLease, str(device.id))
                lease_released = bool(lease is not None and str(lease.state) == "RELEASED")
        except Exception as exc:
            cleanup_errors.append(f"LEASE_RELEASE:{type(exc).__name__}:{exc}")

    post_mode = capture_authority_mode()
    post_version = str(settings.capture_engine_version)
    post_prod_enabled = bool(settings.capture_v2_production_enabled)
    with runner.session_factory() as db:
        gaps = list(db.scalars(select(CaptureGap).where(
            CaptureGap.capture_session_id == bootstrap.capture_session_id
        )))
    unresolved = [str(g.id) for g in gaps if g.recovered_at is None]

    checks = (
        GateCheck("pre_v1_config_authority", pre_mode == "V1" and pre_version == "V1" and not pre_prod_enabled,
                  {"mode": "V1", "version": "V1", "production_v2": False},
                  {"mode": pre_mode, "version": pre_version, "production_v2": pre_prod_enabled}),
        GateCheck("validation_v2_real_single_producer", active_owned_count == 1 and active_exact_count == 1,
                  {"owned": 1, "exact": 1}, {"owned": active_owned_count, "exact": active_exact_count}),
        GateCheck("validation_v2_real_active_lease", active_lease, True, active_lease),
        GateCheck("validation_v2_real_segments_observed", active_segment_count > 0, ">0", active_segment_count),
        GateCheck("validation_v2_real_durable_segment_observed", active_durable_count > 0, ">0", active_durable_count),
        GateCheck("validation_v2_runtime_no_exception", not runtime_errors, [], runtime_errors),
        GateCheck("rollback_exact_producer_removed", producer_gone, True, producer_gone),
        GateCheck("rollback_lease_released", lease_released, True, lease_released),
        GateCheck("rollback_no_unresolved_gap", not unresolved, [], unresolved),
        GateCheck("rollback_cleanup_no_exception", not cleanup_errors, [], cleanup_errors),
        GateCheck("post_v1_config_authority_restored",
                  post_mode == "V1" and post_version == "V1" and not post_prod_enabled,
                  {"mode": "V1", "version": "V1", "production_v2": False},
                  {"mode": post_mode, "version": post_version, "production_v2": post_prod_enabled}),
    )

    facts = {
        "scope": "VALIDATION_REHEARSAL_ONLY",
        "release_gate_effect": "PROVES_REAL_DUT_V2_VALIDATION_AUTHORITY_ROLLBACK_MECHANICS_NOT_PRODUCTION_CUTOVER",
        "capture_session_id": bootstrap.capture_session_id,
        "capture_epoch_id": bootstrap.ownership.capture_epoch_id,
        "pre": {"authority_mode": pre_mode, "capture_engine_version": pre_version,
                "capture_v2_production_enabled": pre_prod_enabled},
        "validation_v2_active": {
            "owned_count": active_owned_count,
            "exact_producer_count": active_exact_count,
            "lease_active": active_lease,
            "segment_count": active_segment_count,
            "durable_segment_count": active_durable_count,
        },
        "rollback": {
            "producer_gone": producer_gone,
            "lease_released": lease_released,
            "unresolved_gaps": unresolved,
            "cleanup_errors": cleanup_errors,
        },
        "post": {"authority_mode": post_mode, "capture_engine_version": post_version,
                 "capture_v2_production_enabled": post_prod_enabled},
        "limitations": [
            "Production V2 was never enabled",
            "This proves real-DUT validation authority rollback mechanics, not production service cutover",
            "PR ready/merge/cutover still require separate explicit authorization",
        ],
    }

    collector = GateEvidenceCollector(
        session_factory=runner.session_factory,
        adapter=runner.adapter,
        object_root=runner.object_root,
        repo_root=runner.repo_root,
    )
    await collector.collect(
        paths=paths,
        gate_id=gate_id,
        capture_session_id=bootstrap.capture_session_id,
        device_id=str(device.id),
        facts=facts,
    )
    return GateCaseResult(
        gate_id=gate_id,
        verdict=_verdict(checks),
        checks=checks,
        summary="R7 real-DUT validation-scope V2 authority rollback rehearsal",
        evidence_bundle=str(paths.case_dir),
        facts=facts,
    )
