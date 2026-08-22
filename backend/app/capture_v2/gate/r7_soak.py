from __future__ import annotations

import time
from typing import Any

from sqlalchemy import select

from app.capture_v2.c_bridge import CaptureV2CBridge
from app.capture_v2.db_models import CaptureGap, CaptureLease, CaptureSegment
from app.capture_v2.gate.evidence import GateEvidenceCollector
from app.capture_v2.gate.models import GateCaseResult, GateCheck, GateRunPaths, GateVerdict


def _verdict(checks: tuple[GateCheck, ...]) -> GateVerdict:
    if any(c.passed is False for c in checks):
        return GateVerdict.FAIL
    if any(c.passed is None for c in checks):
        return GateVerdict.INCONCLUSIVE
    return GateVerdict.PASS


async def run_r7_validation_soak(
    runner,
    *,
    reproduction_session_id: str,
    device: Any,
    worker_id: str,
    gate_id: str,
    transport: str,
    duration_seconds: float,
    cycle_interval_seconds: float,
) -> GateCaseResult:
    """Long validation-state stability soak; never a production shadow/rollback PASS.

    Production V2 stays disabled by the outer control policy. This Gate validates
    the same real-DUT capture/lease/transfer components for an extended period and
    proves exact cleanup. It deliberately reports only supporting R7 evidence; an
    actual V2_ACTIVE shadow/cutover/rollback still requires explicit authorization.
    """
    duration_seconds = max(30.0, min(float(duration_seconds), 1800.0))
    cycle_interval_seconds = max(0.25, min(float(cycle_interval_seconds), 5.0))
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
    start = time.monotonic()
    cycles = []
    samples: list[dict] = []
    runtime_errors: list[str] = []
    cleanup_errors: list[str] = []
    producer_gone = False
    lease_released = False
    exit_stats = None

    try:
        # Sample in bounded chunks so ownership/pressure stability is independently
        # observed throughout the soak instead of only once at the end.
        remaining = duration_seconds
        while remaining > 0:
            chunk = min(15.0, remaining)
            batch = await session.drain_for(
                duration_seconds=chunk,
                cycle_interval_seconds=cycle_interval_seconds,
            )
            cycles.extend(batch)
            session.components["lease"].validate(session.token)
            owned = await session.components["producer"].inspect_owned()
            exact = [
                p for p in owned
                if int(p.pid) == int(bootstrap.ownership.producer.pid)
                and int(p.process_starttime) == int(bootstrap.ownership.producer.process_starttime)
            ]
            last_pressure = batch[-1].pressure if batch else None
            samples.append({
                "lease_epoch": session.token.lease_epoch,
                "control_authority": session.control_authority,
                "owned_count": len(owned),
                "exact_producer_count": len(exact),
                "pressure_state": getattr(last_pressure, "state", None),
                "pressure_reasons": list(getattr(last_pressure, "reasons", ()) or ()),
            })
            remaining -= chunk
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
            exit_stats = await session.components["producer"].read_exit_stats(
                bootstrap.ownership.producer.capture_epoch or bootstrap.ownership.capture_epoch_token
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

    elapsed = time.monotonic() - start
    with runner.session_factory() as db:
        segments = list(db.scalars(select(CaptureSegment).where(
            CaptureSegment.capture_session_id == bootstrap.capture_session_id
        )))
        gaps = list(db.scalars(select(CaptureGap).where(
            CaptureGap.capture_session_id == bootstrap.capture_session_id
        )))

    verified_ids: list[str] = []
    non_durable: list[dict] = []
    for row in segments:
        durable = row.state in {"PERSISTED", "ACK_PENDING", "ACKED", "REMOTE_DELETED"}
        if not durable:
            non_durable.append({"id": str(row.id), "state": row.state, "error": row.last_error_code})
            continue
        if row.storage_key and row.server_size and row.sha256:
            try:
                if session.components["store"].verify(
                    storage_key=row.storage_key,
                    size=int(row.server_size),
                    sha256=row.sha256,
                ):
                    verified_ids.append(str(row.id))
                elif row.state in {"ACKED", "REMOTE_DELETED"}:
                    non_durable.append({"id": str(row.id), "state": row.state, "error": "SERVER_COPY_MISSING"})
            except Exception as exc:
                non_durable.append({"id": str(row.id), "state": row.state, "error": f"STORE_VERIFY:{type(exc).__name__}"})

    unresolved_gaps = [
        {"id": str(g.id), "channel": g.channel, "reason_code": g.reason_code,
         "certainty": g.certainty, "recovered_at": g.recovered_at.isoformat() if g.recovered_at else None}
        for g in gaps if g.recovered_at is None
    ]
    total_errors = sum(int(x.pump.errors) for x in cycles)
    total_sealed = sum(int(x.pump.sealed) for x in cycles)
    total_transferred = sum(int(x.pump.transferred) for x in cycles)
    total_acked = sum(int(x.pump.acked) for x in cycles)
    total_deleted = sum(int(x.pump.deleted) for x in cycles)
    critical_samples = [s for s in samples if str(s.get("pressure_state") or "").upper() == "CRITICAL"]
    bad_owner_samples = [s for s in samples if s.get("owned_count") != 1 or s.get("exact_producer_count") != 1]
    lost_authority_samples = [s for s in samples if s.get("control_authority") != "ACTIVE"]
    kernel_drop = getattr(exit_stats, "packets_dropped_kernel", None) if exit_stats is not None else None

    checks = (
        GateCheck("soak_runtime_no_exception", not runtime_errors, [], runtime_errors),
        GateCheck("soak_elapsed_full_window", elapsed >= duration_seconds * 0.95, f">={duration_seconds * 0.95:.1f}s", round(elapsed, 3)),
        GateCheck("soak_has_repeated_samples", len(samples) >= 2, ">=2", len(samples)),
        GateCheck("lease_authority_stayed_active", not lost_authority_samples, [], lost_authority_samples),
        GateCheck("exact_single_producer_throughout", not bad_owner_samples, [], bad_owner_samples),
        GateCheck("pump_zero_errors", total_errors == 0, 0, total_errors),
        GateCheck("no_critical_spool_pressure", not critical_samples, [], critical_samples),
        GateCheck("no_unresolved_capture_gap", not unresolved_gaps, [], unresolved_gaps),
        GateCheck("observed_segments_not_non_durable", not non_durable, [], non_durable),
        GateCheck("observed_acked_segments_server_durable",
                  all((s.state not in {"ACKED", "REMOTE_DELETED"}) or str(s.id) in verified_ids for s in segments),
                  "all ACKED/REMOTE_DELETED verified", verified_ids),
        GateCheck("producer_cleanup_exact_identity", producer_gone, True, producer_gone),
        GateCheck("lease_cleanup_released", lease_released, True, lease_released),
        GateCheck("cleanup_no_exception", not cleanup_errors, [], cleanup_errors),
    )

    facts = {
        "release_gate_effect": "R7_SUPPORTING_VALIDATION_SOAK_ONLY_NOT_SHADOW_OR_ROLLBACK_PASS",
        "duration_requested_seconds": duration_seconds,
        "elapsed_seconds": elapsed,
        "capture_session_id": bootstrap.capture_session_id,
        "capture_epoch_id": bootstrap.ownership.capture_epoch_id,
        "sample_count": len(samples),
        "samples": samples,
        "pipeline": {
            "cycles": len(cycles),
            "sealed": total_sealed,
            "transferred": total_transferred,
            "acked": total_acked,
            "deleted": total_deleted,
            "errors": total_errors,
            "segment_count": len(segments),
            "verified_segment_ids": verified_ids,
            "non_durable": non_durable,
        },
        "gaps": {"count": len(gaps), "unresolved": unresolved_gaps},
        "exit_stats": {
            "packets_captured": getattr(exit_stats, "packets_captured", None),
            "packets_received": getattr(exit_stats, "packets_received", None),
            "packets_dropped_kernel": kernel_drop,
        } if exit_stats is not None else None,
        "cleanup": {"producer_gone": producer_gone, "lease_released": lease_released, "errors": cleanup_errors},
        "limitations": [
            "Production V2 remained disabled",
            "This is validation-state soak evidence, not V1/V2 shadow equivalence",
            "This does not execute V2_ACTIVE -> ROLLED_BACK_V1",
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
        summary="R7 validation-state supporting soak (Production V2 remains disabled)",
        evidence_bundle=str(paths.case_dir),
        facts=facts,
    )
