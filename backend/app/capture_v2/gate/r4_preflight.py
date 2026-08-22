from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.capture_v2.c_bridge import CaptureV2CBridge
from app.capture_v2.d_bridge import CaptureV2DSession
from app.capture_v2.db_models import CaptureLease, CaptureSegment, ReadinessSnapshot
from app.capture_v2.enums import ReadinessStatus
from app.capture_v2.gate.evidence import GateEvidenceCollector
from app.capture_v2.gate.models import GateCaseResult, GateCheck, GateRunPaths, GateVerdict
from app.capture_v2.readiness.stage1 import CapturePathChecks
from app.capture_v2.readiness.watchdog import WatchdogInputs
from app.reproduction.fxs_event_monitor import FULL_DEBUG_DISABLE, FULL_DEBUG_ENABLE


def _all_pass(checks: tuple[GateCheck, ...]) -> GateVerdict:
    if any(c.passed is False for c in checks):
        return GateVerdict.FAIL
    if any(c.passed is None for c in checks):
        return GateVerdict.INCONCLUSIVE
    return GateVerdict.PASS


async def run_r4_no_handset_preflight(
    runner,
    *,
    reproduction_session_id: str,
    device: Any,
    worker_id: str,
    gate_id: str,
    duration_seconds: float = 12.0,
    transport: str = "scp",
) -> GateCaseResult:
    """Real-DUT Stage-1 readiness preflight that requires no handset activity.

    The preflight proves the infrastructure side of R4 using the real DUT and the
    real PostgreSQL/store path: ownership, one producer, Voice Context, PCAP
    segment transfer/durability, AIM PTY/debug control, reversible PCM diagnostic
    control, watchdog healthy/revoke/recover semantics and deterministic cleanup.

    It never waits for OFFHOOK/DTMF/ONHOOK and therefore can never replace the
    physical R4-01 FXS semantic Gate.
    """
    duration_seconds = max(7.0, min(float(duration_seconds), 60.0))
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
    d_session = CaptureV2DSession(
        capture_session_id=bootstrap.capture_session_id,
        session_factory=runner.session_factory,
        effective_profile=bootstrap.effective_profile.model_dump(),
    )
    paths = GateRunPaths.create(runner.output_root, gate_id, str(device.id))

    debug_enable_acked = 0
    debug_disable_acked = 0
    pcm_enable_acked = 0
    pcm_disable_acked = 0
    cleanup_errors: list[str] = []
    runtime_errors: list[str] = []
    cycles = ()
    owned_before = []
    segments: list[CaptureSegment] = []
    store_verified_ids: list[str] = []
    actual_checks: CapturePathChecks | None = None
    healthy_watchdog = None
    revoke_watchdog = None
    ready_decision = None
    recovered_decision = None
    producer_gone = False
    lease_released = False
    exit_stats = None

    gateway = bootstrap.voice_context.gateway_ip
    pcm_on = (
        f"voip dsp diag set {gateway} 40000 1 pcm_rx on",
        f"voip dsp diag set {gateway} 50000 1 pcm_tx on",
    )
    pcm_off = (
        f"voip dsp diag set {gateway} 40000 1 pcm_rx off",
        f"voip dsp diag set {gateway} 50000 1 pcm_tx off",
    )

    try:
        # Real AIM PTY + reversible debug controls. No FXS event is required.
        await runner.adapter.ensure_aim_session_ready()
        for command in FULL_DEBUG_ENABLE:
            await runner.adapter.execute_cli(command)
            debug_enable_acked += 1
        for command in pcm_on:
            await runner.adapter.execute_cli(command)
            pcm_enable_acked += 1

        cycles = await session.drain_for(
            duration_seconds=duration_seconds,
            cycle_interval_seconds=0.5,
        )

        # Validate current lease before interpreting any DUT facts.
        session.components["lease"].validate(session.token)
        owned_before = await session.components["producer"].inspect_owned()
        producer_matches = [
            p for p in owned_before
            if int(p.pid) == int(bootstrap.ownership.producer.pid)
            and int(p.process_starttime) == int(bootstrap.ownership.producer.process_starttime)
        ]

        with runner.session_factory() as db:
            segments = list(db.scalars(select(CaptureSegment).where(
                CaptureSegment.capture_session_id == bootstrap.capture_session_id
            )))

        for row in segments:
            if not row.storage_key or not row.server_size or not row.sha256:
                continue
            try:
                if session.components["store"].verify(
                    storage_key=row.storage_key,
                    size=int(row.server_size),
                    sha256=row.sha256,
                ):
                    store_verified_ids.append(str(row.id))
            except Exception:
                pass

        total_errors = sum(int(x.pump.errors) for x in cycles)
        total_transferred = sum(int(x.pump.transferred) for x in cycles)
        total_acked = sum(int(x.pump.acked) for x in cycles)
        total_deleted = sum(int(x.pump.deleted) for x in cycles)
        durable = [x for x in segments if x.state in {"PERSISTED", "ACK_PENDING", "ACKED", "REMOTE_DELETED"}]
        full_chain = [x for x in segments if x.state == "REMOTE_DELETED" and x.acked_at and x.remote_deleted_at]
        unacked = [x for x in segments if x.state not in {"ACKED", "REMOTE_DELETED"}]
        pressure_critical = any(str(x.pressure.state).upper() == "CRITICAL" for x in cycles)

        lease_active = session.control_authority == "ACTIVE"
        exactly_one = len(owned_before) == 1 and len(producer_matches) == 1
        voice_ready = bool(
            bootstrap.voice_context.gateway_ip
            and bootstrap.voice_context.voice_vlan_id
            and bootstrap.voice_context.interface
        )
        pcap_ready = bool(cycles and total_errors == 0 and durable)
        fxs_ready = debug_enable_acked == len(FULL_DEBUG_ENABLE)
        pcm_ready = pcm_enable_acked == len(pcm_on)
        store_ready = bool(store_verified_ids)
        transfer_ready = bool(full_chain and total_transferred > 0 and total_acked > 0 and total_deleted > 0)
        storage_guard_ready = not pressure_critical and not unacked

        healthy_watchdog = d_session.evaluate_watchdog(WatchdogInputs(
            lease_active=lease_active,
            producer_alive=len(producer_matches) == 1,
            producer_count=len(owned_before),
            fxs_reader_alive=fxs_ready,
            server_store_healthy=store_ready,
            transfer_healthy=transfer_ready,
            spool_critical=pressure_critical,
        ))
        actual_checks = CapturePathChecks(
            lease_active=lease_active,
            exactly_one_producer=exactly_one,
            voice_context_ready=voice_ready,
            pcap_ready=pcap_ready,
            fxs_ready=fxs_ready,
            pcm_control_ready=pcm_ready,
            server_store_ready=store_ready,
            transfer_ready=transfer_ready,
            storage_guard_ready=storage_guard_ready,
            watchdog_ready=healthy_watchdog.healthy,
        )
        ready_decision = d_session.evaluate_stage1(actual_checks)

        # Real PostgreSQL persistence of the watchdog fail-closed transition.
        # This injects only the watchdog input value, not a DUT/server failure.
        revoke_watchdog = d_session.evaluate_watchdog(WatchdogInputs(
            lease_active=lease_active,
            producer_alive=len(producer_matches) == 1,
            producer_count=len(owned_before),
            fxs_reader_alive=fxs_ready,
            server_store_healthy=False,
            transfer_healthy=transfer_ready,
            spool_critical=pressure_critical,
        ))
        recovered_decision = d_session.evaluate_stage1(actual_checks)
    except Exception as exc:
        runtime_errors.append(f"{type(exc).__name__}:{exc}")
    finally:
        # Symmetric controls first: disable PCM and verbose AIM debug regardless
        # of where the preflight failed.
        for command in pcm_off:
            try:
                await runner.adapter.execute_cli(command)
                pcm_disable_acked += 1
            except Exception as exc:
                cleanup_errors.append(f"PCM_OFF:{type(exc).__name__}:{exc}")
        for command in FULL_DEBUG_DISABLE:
            try:
                await runner.adapter.execute_cli(command)
                debug_disable_acked += 1
            except Exception as exc:
                cleanup_errors.append(f"DEBUG_OFF:{type(exc).__name__}:{exc}")

        try:
            await session.stop_lease_renewer(release_lease=False)
        except Exception as exc:
            cleanup_errors.append(f"STOP_RENEWER:{type(exc).__name__}:{exc}")
        try:
            await session.components["producer"].stop_identity(
                session.token, bootstrap.ownership.producer
            )
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

    with runner.session_factory() as db:
        readiness_rows = list(db.scalars(select(ReadinessSnapshot).where(
            ReadinessSnapshot.capture_session_id == bootstrap.capture_session_id
        ).order_by(ReadinessSnapshot.created_at)))
    readiness_statuses = [str(x.status) for x in readiness_rows]

    total_errors = sum(int(x.pump.errors) for x in cycles) if cycles else 0
    total_transferred = sum(int(x.pump.transferred) for x in cycles) if cycles else 0
    total_acked = sum(int(x.pump.acked) for x in cycles) if cycles else 0
    total_deleted = sum(int(x.pump.deleted) for x in cycles) if cycles else 0
    checks = (
        GateCheck("preflight_runtime_no_exception", not runtime_errors, [], runtime_errors),
        GateCheck("stage1_all_real_checks_ready", bool(actual_checks and all(actual_checks.as_dict().values())), True,
                  actual_checks.as_dict() if actual_checks else None),
        GateCheck("stage1_persisted_ready", bool(ready_decision and ready_decision.status == ReadinessStatus.READY),
                  ReadinessStatus.READY.value, ready_decision.status.value if ready_decision else None),
        GateCheck("watchdog_healthy_on_real_inputs", bool(healthy_watchdog and healthy_watchdog.healthy), True,
                  list(healthy_watchdog.reasons) if healthy_watchdog else None),
        GateCheck("watchdog_fail_closed_revoke", bool(revoke_watchdog and not revoke_watchdog.healthy and "SERVER_STORE_UNHEALTHY" in revoke_watchdog.reasons),
                  "SERVER_STORE_UNHEALTHY", list(revoke_watchdog.reasons) if revoke_watchdog else None),
        GateCheck("readiness_recovered_after_revoke", bool(recovered_decision and recovered_decision.status == ReadinessStatus.READY),
                  ReadinessStatus.READY.value, recovered_decision.status.value if recovered_decision else None),
        GateCheck("readiness_db_contains_revoke_and_ready", "REVOKED" in readiness_statuses and readiness_statuses[-1:] == ["READY"],
                  "...REVOKED...READY", readiness_statuses),
        GateCheck("segment_pipeline_zero_errors", total_errors == 0, 0, total_errors),
        GateCheck("segment_pipeline_full_chain", total_transferred > 0 and total_acked > 0 and total_deleted > 0,
                  ">0 transferred/acked/deleted", {"transferred": total_transferred, "acked": total_acked, "deleted": total_deleted}),
        GateCheck("server_durable_objects_verified", bool(store_verified_ids), ">0", store_verified_ids),
        GateCheck("pcm_control_symmetric", pcm_enable_acked == len(pcm_on) and pcm_disable_acked == len(pcm_off),
                  {"on": len(pcm_on), "off": len(pcm_off)}, {"on": pcm_enable_acked, "off": pcm_disable_acked}),
        GateCheck("aim_debug_control_symmetric", debug_enable_acked == len(FULL_DEBUG_ENABLE) and debug_disable_acked == len(FULL_DEBUG_DISABLE),
                  {"on": len(FULL_DEBUG_ENABLE), "off": len(FULL_DEBUG_DISABLE)}, {"on": debug_enable_acked, "off": debug_disable_acked}),
        GateCheck("producer_cleanup_exact_identity", producer_gone, True, producer_gone),
        GateCheck("lease_cleanup_released", lease_released, True, lease_released),
        GateCheck("cleanup_no_exception", not cleanup_errors, [], cleanup_errors),
    )

    facts = {
        "release_gate_effect": "R4_NON_PHYSICAL_PREFLIGHT_ONLY_NOT_PHYSICAL_FXS_PASS",
        "capture_session_id": bootstrap.capture_session_id,
        "capture_epoch_id": bootstrap.ownership.capture_epoch_id,
        "voice_context": {
            "gateway_ip": bootstrap.voice_context.gateway_ip,
            "voice_vlan_id": bootstrap.voice_context.voice_vlan_id,
            "interface": bootstrap.voice_context.interface,
        },
        "stage1_checks": actual_checks.as_dict() if actual_checks else None,
        "readiness_statuses": readiness_statuses,
        "cycles": len(cycles),
        "pipeline": {
            "errors": total_errors,
            "transferred": total_transferred,
            "acked": total_acked,
            "deleted": total_deleted,
            "store_verified_ids": store_verified_ids,
        },
        "controls": {
            "debug_enable_acked": debug_enable_acked,
            "debug_disable_acked": debug_disable_acked,
            "pcm_enable_acked": pcm_enable_acked,
            "pcm_disable_acked": pcm_disable_acked,
        },
        "cleanup": {
            "producer_gone": producer_gone,
            "lease_released": lease_released,
            "errors": cleanup_errors,
        },
        "runtime_errors": runtime_errors,
        "exit_stats": {
            "packets_captured": getattr(exit_stats, "packets_captured", None),
            "packets_received": getattr(exit_stats, "packets_received", None),
            "packets_dropped_kernel": getattr(exit_stats, "packets_dropped_kernel", None),
        } if exit_stats is not None else None,
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
        verdict=_all_pass(checks),
        checks=checks,
        summary="R4-00 no-handset real-DUT capture/readiness/watchdog preflight",
        evidence_bundle=str(paths.case_dir),
        facts=facts,
    )
