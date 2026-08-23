from __future__ import annotations

import asyncio
import os
import shlex
import struct
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from app.capture_v2.c_bridge import CaptureV2CBridge
from app.capture_v2.coverage.calculator import EvidenceInterval
from app.capture_v2.db_models import (
    CaptureAttempt,
    CaptureLease,
    CaptureSegment,
    CoverageInterval,
    CoverageTrack,
    CoverageWindow,
)
from app.capture_v2.enums import CoverageIntervalType, ReadinessStatus
from app.capture_v2.finalizer import CaptureV2CaptureFinalizer
from app.capture_v2.fxs.sanitizer import RawFxsEvent
from app.capture_v2.gate.evidence import GateEvidenceCollector
from app.capture_v2.gate.models import GateCaseResult, GateCheck, GateRunPaths, GateVerdict
from app.capture_v2.gate.r4_preflight import _probe_local_store, _probe_scp_transfer
from app.capture_v2.readiness.stage1 import CapturePathChecks
from app.capture_v2.readiness.watchdog import WatchdogInputs
from app.capture_v2.runtime_coordinator import CaptureV2RuntimeCoordinator
from app.reproduction.fxs_event_monitor import FxsEventMonitor, FULL_DEBUG_DISABLE, FULL_DEBUG_ENABLE


def _parse_device_source_ts(timestamp: str, offset: str) -> datetime:
    return datetime.strptime(f"{timestamp} {offset}", "%Y-%m-%d %H:%M:%S.%f %z")


async def _device_floor_time(adapter, offset: str) -> datetime:
    result = await adapter.execute_shell("date '+%Y-%m-%d %H:%M:%S'", retries=0)
    text = str(result.stdout or "").strip().splitlines()[-1]
    return datetime.strptime(f"{text} {offset}", "%Y-%m-%d %H:%M:%S %z")


def _ordered_basic_sequence(events: list[dict[str, Any]]) -> bool:
    state = 0
    for event in events:
        kind = str(event.get("event") or "").upper()
        if state == 0 and kind == "OFFHOOK":
            state = 1
        elif state == 1 and kind == "DTMF":
            state = 2
        elif state == 2 and kind == "ONHOOK":
            return True
    return False


def _pcap_udp_timestamps(path: Path, ports: set[int]) -> dict[int, list[datetime]]:
    out = {int(port): [] for port in ports}
    data = path.read_bytes()
    if len(data) < 24:
        return out
    magic = data[:4]
    if magic == b"\xd4\xc3\xb2\xa1":
        endian, scale = "<", 1_000_000
    elif magic == b"\xa1\xb2\xc3\xd4":
        endian, scale = ">", 1_000_000
    elif magic == b"\x4d\x3c\xb2\xa1":
        endian, scale = "<", 1_000_000_000
    elif magic == b"\xa1\xb2\x3c\x4d":
        endian, scale = ">", 1_000_000_000
    else:
        return out

    pos = 24
    while pos + 16 <= len(data):
        ts_sec, ts_frac, incl_len, _orig_len = struct.unpack_from(endian + "IIII", data, pos)
        pos += 16
        if incl_len < 0 or pos + incl_len > len(data):
            break
        frame = data[pos:pos + incl_len]
        pos += incl_len
        if len(frame) < 14:
            continue
        eth_type = int.from_bytes(frame[12:14], "big")
        l3 = 14
        while eth_type in (0x8100, 0x88A8, 0x9100):
            if len(frame) < l3 + 4:
                break
            eth_type = int.from_bytes(frame[l3 + 2:l3 + 4], "big")
            l3 += 4
        if eth_type != 0x0800 or len(frame) < l3 + 20:
            continue
        ihl = (frame[l3] & 0x0F) * 4
        if ihl < 20 or len(frame) < l3 + ihl + 8 or frame[l3 + 9] != 17:
            continue
        udp = l3 + ihl
        sport = int.from_bytes(frame[udp:udp + 2], "big")
        dport = int.from_bytes(frame[udp + 2:udp + 4], "big")
        matched = [p for p in ports if p == sport or p == dport]
        if not matched:
            continue
        ts = datetime.fromtimestamp(ts_sec + (ts_frac / scale), tz=timezone.utc)
        for port in matched:
            out[int(port)].append(ts)
    return out


def _stream_intervals(times: list[datetime], *, source_kind: str, source_id: str,
                      max_gap_ms: int = 250) -> tuple[list[EvidenceInterval], dict[str, Any]]:
    values = sorted(set(times))
    if len(values) < 2:
        return [], {"packet_count": len(values), "max_interpacket_gap_ms": None, "groups": 0}
    groups: list[list[datetime]] = [[values[0]]]
    max_gap = 0.0
    for ts in values[1:]:
        gap_ms = (ts - groups[-1][-1]).total_seconds() * 1000.0
        max_gap = max(max_gap, gap_ms)
        if gap_ms > max_gap_ms:
            groups.append([ts])
        else:
            groups[-1].append(ts)
    evidence: list[EvidenceInterval] = []
    for index, group in enumerate(groups):
        if len(group) < 2 or group[-1] <= group[0]:
            continue
        evidence.append(EvidenceInterval(
            start=group[0],
            end=group[-1],
            interval_type=CoverageIntervalType.COVERED,
            source_kind=source_kind,
            source_id=f"{source_id}:{index}",
            certainty="CONFIRMED",
            details={
                "packet_count": len(group),
                "max_allowed_interpacket_gap_ms": max_gap_ms,
            },
        ))
    return evidence, {
        "packet_count": len(values),
        "first_packet_ts": values[0].isoformat(),
        "last_packet_ts": values[-1].isoformat(),
        "max_interpacket_gap_ms": round(max_gap, 3),
        "groups": len(groups),
    }


async def run_r5_real_live_coverage(
    runner,
    *,
    reproduction_session_id: str,
    device: Any,
    worker_id: str,
    gate_id: str,
    duration_seconds: float = 180.0,
    transport: str = "scp",
) -> GateCaseResult:
    """R5 real-call Coverage Ledger finalization over the non-production V2 runtime path.

    Production V2 remains disabled. The Gate uses the same D/E/F runtime coordinator,
    a real continuous CaptureEpoch, real AIM/FXS events, real PCM diag controls, and
    real durable PCAP segments. PCAP coverage is computed by the production E bridge;
    FXS coverage is the proven live monitor interval; PCM_RX/TX coverage is derived
    only from actual UDP 40000/50000 packets found in the durable PCAP files.
    """
    duration_seconds = max(45.0, min(float(duration_seconds), 300.0))
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
    coordinator = CaptureV2RuntimeCoordinator(
        session_factory=runner.session_factory,
        capture_session_id=bootstrap.capture_session_id,
        effective_profile=bootstrap.effective_profile.model_dump(),
    )
    paths = GateRunPaths.create(runner.output_root, gate_id, str(device.id))
    transcript_path = paths.dut_dir / "aim_fxs_transcript.log"

    gateway = bootstrap.voice_context.gateway_ip
    pcm_on = (
        f"voip dsp diag set {gateway} 40000 1 pcm_rx on",
        f"voip dsp diag set {gateway} 50000 1 pcm_tx on",
    )
    pcm_off = (
        f"voip dsp diag set {gateway} 40000 1 pcm_rx off",
        f"voip dsp diag set {gateway} 50000 1 pcm_tx off",
    )

    tz_result = await runner.adapter.execute_shell("date +%z", retries=0)
    device_offset = (str(tz_result.stdout or "").strip().splitlines()[-1:] or [""])[0].strip()
    timezone_proven = len(device_offset) == 5 and device_offset[0] in "+-" and device_offset[1:].isdigit()
    if not timezone_proven:
        device_offset = "+0000"

    monitor = FxsEventMonitor(read_aim_chunk=lambda: None, write_aim=lambda _: None)
    monitor.start(enable_debug=False)
    transcript: list[str] = []
    observed: list[dict[str, Any]] = []
    runtime_errors: list[str] = []
    cleanup_errors: list[str] = []
    debug_enable_acked = 0
    debug_disable_acked = 0
    pcm_enable_acked = 0
    pcm_disable_acked = 0
    store_probe_ok = False
    transfer_probe_ok = False
    readiness_status = None
    physical_complete = False
    target_attempt_id: str | None = None
    final_onhook_ts: datetime | None = None
    fxs_monitor_start: datetime | None = None
    fxs_monitor_end: datetime | None = None
    finalizer_result = None
    coverage_result = None
    coverage_window_id: str | None = None
    producer_gone = False
    lease_released = False
    pcm_facts: dict[str, Any] = {}
    stage1_checks: CapturePathChecks | None = None

    try:
        await runner.adapter.ensure_aim_session_ready()
        for command in FULL_DEBUG_ENABLE:
            await runner.adapter.execute_cli(command)
            debug_enable_acked += 1
        for command in pcm_on:
            await runner.adapter.execute_cli(command)
            pcm_enable_acked += 1

        store_probe_ok, _store_probe_error = _probe_local_store(session.components["store"])
        transfer_probe_ok, _transfer_probe_bytes, _transfer_probe_error = await _probe_scp_transfer(runner.adapter)

        await session.start_lease_renewer()
        initial = await session.drain_once()
        session.components["lease"].validate(session.token)
        owned = await session.components["producer"].inspect_owned()
        producer_matches = [
            p for p in owned
            if int(p.pid) == int(bootstrap.ownership.producer.pid)
            and int(p.process_starttime) == int(bootstrap.ownership.producer.process_starttime)
        ]
        active_path = f"/tmp/aivoip_capture/epochs/{bootstrap.ownership.capture_epoch_token}/active"
        active_dir_exists = (
            await session.components["reader"].run(
                f"[ -d {shlex.quote(active_path)} ] && echo 1 || echo 0"
            )
        ).strip() == "1"
        pressure_critical = str(initial.pressure.state).upper() == "CRITICAL"
        watchdog = coordinator.stack.d.evaluate_watchdog(WatchdogInputs(
            lease_active=session.control_authority == "ACTIVE",
            producer_alive=len(producer_matches) == 1,
            producer_count=len(owned),
            fxs_reader_alive=debug_enable_acked == len(FULL_DEBUG_ENABLE),
            server_store_healthy=store_probe_ok,
            transfer_healthy=transfer_probe_ok,
            spool_critical=pressure_critical,
        ))
        stage1_checks = CapturePathChecks(
            lease_active=session.control_authority == "ACTIVE",
            exactly_one_producer=len(owned) == 1 and len(producer_matches) == 1,
            voice_context_ready=bool(
                bootstrap.voice_context.gateway_ip
                and bootstrap.voice_context.voice_vlan_id
                and bootstrap.voice_context.interface
            ),
            pcap_ready=initial.pump.errors == 0 and len(producer_matches) == 1 and active_dir_exists,
            fxs_ready=debug_enable_acked == len(FULL_DEBUG_ENABLE),
            pcm_control_ready=pcm_enable_acked == len(pcm_on),
            server_store_ready=store_probe_ok,
            transfer_ready=transfer_probe_ok,
            storage_guard_ready=not pressure_critical,
            watchdog_ready=watchdog.healthy,
        )
        readiness_status = coordinator.arm_to_watching(stage1_checks)
        if readiness_status != ReadinessStatus.READY:
            raise RuntimeError(f"R5_STAGE1_NOT_READY:{readiness_status}")

        # Clear any buffered pre-Gate AIM text, then start the conservative FXS
        # availability interval. +1 second on the floor timestamp avoids claiming
        # availability before the reader was actually ready.
        prearm_deadline = asyncio.get_running_loop().time() + 3.0
        while asyncio.get_running_loop().time() < prearm_deadline:
            chunk = await runner.adapter.read_aim_chunk(timeout=0.10)
            if chunk:
                transcript.append(chunk)
            await session.drain_once()
            await asyncio.sleep(0.10)
        fxs_monitor_start = (await _device_floor_time(runner.adapter, device_offset)) + timedelta(seconds=1)

        loop = asyncio.get_running_loop()
        deadline = loop.time() + duration_seconds
        next_drain = loop.time()
        while loop.time() < deadline:
            now = loop.time()
            if now >= next_drain:
                await session.drain_once()
                next_drain = now + 0.5
            chunk = await runner.adapter.read_aim_chunk(timeout=0.25)
            if not chunk:
                await asyncio.sleep(0.02)
                continue
            transcript.append(chunk)
            for event in monitor.feed(chunk):
                source_ts = _parse_device_source_ts(event.timestamp, device_offset)
                ids = coordinator.ingest_fxs(RawFxsEvent(
                    source_ts=source_ts,
                    event=event.event,
                    digit=event.digit,
                    line=event.line,
                ))
                observed.append({
                    "timestamp": event.timestamp,
                    "source_ts": source_ts.isoformat(),
                    "line": event.line,
                    "event": event.event,
                    "digit": event.digit,
                })
                if str(event.event).upper() == "DTMF" and target_attempt_id is None and ids:
                    target_attempt_id = ids[0]
                    coordinator.mark_target_confirmed(
                        capture_attempt_id=target_attempt_id,
                        source_ts=source_ts,
                        reason="R5_REAL_FXS_DTMF_TARGET",
                    )
                if target_attempt_id and str(event.event).upper() == "ONHOOK":
                    with runner.session_factory() as db:
                        attempt = db.get(CaptureAttempt, target_attempt_id)
                        if attempt is not None and str(attempt.state) == "ENDED":
                            final_onhook_ts = source_ts
                            physical_complete = _ordered_basic_sequence(observed)
                            break
            if final_onhook_ts is not None:
                break

        if not physical_complete or target_attempt_id is None or final_onhook_ts is None:
            raise RuntimeError("R5_REAL_CALL_SEQUENCE_NOT_COMPLETED")

        # Keep all channels armed beyond the policy's 10-second post-trigger window.
        post_deadline = loop.time() + 11.5
        next_drain = loop.time()
        while loop.time() < post_deadline:
            now = loop.time()
            if now >= next_drain:
                await session.drain_once()
                next_drain = now + 0.5
            chunk = await runner.adapter.read_aim_chunk(timeout=0.20)
            if chunk:
                transcript.append(chunk)
            await asyncio.sleep(0.02)
        fxs_monitor_end = await _device_floor_time(runner.adapter, device_offset)

        coordinator.begin_evidence_drain(
            source_ts=fxs_monitor_end,
            reason="R5_REAL_CALL_POST_TRIGGER_COMPLETE",
        )
        finalizer = CaptureV2CaptureFinalizer(
            session_factory=runner.session_factory,
            producer_manager=session.components["producer"],
            pump=session.components["pump"],
            lease_manager=session.components["lease"],
        )
        finalizer_result = await finalizer.finalize(
            capture_session_id=bootstrap.capture_session_id,
            capture_epoch_id=bootstrap.ownership.capture_epoch_id,
            capture_epoch_token=bootstrap.ownership.capture_epoch_token,
            producer=bootstrap.ownership.producer,
            token=session.token,
            token_provider=lambda: session.token,
            reason="R5_REAL_CALL_FINALIZE",
        )
        producer_gone = not await session.components["reader"].process_matches(
            pid=bootstrap.ownership.producer.pid,
            starttime=bootstrap.ownership.producer.process_starttime,
        )
        if not finalizer_result.durable:
            raise RuntimeError("R5_EVIDENCE_NOT_DURABLE")
        coordinator.begin_coverage_finalizing(source_ts=fxs_monitor_end)

        with runner.session_factory() as db:
            segments = list(db.scalars(select(CaptureSegment).where(
                CaptureSegment.capture_session_id == bootstrap.capture_session_id,
                CaptureSegment.storage_key.is_not(None),
            )))
        port_times = {40000: [], 50000: []}
        store_root = Path(getattr(session.components["store"], "root"))
        parsed_files = 0
        for segment in segments:
            path = store_root / str(segment.storage_key)
            if not path.is_file() or path.stat().st_size < 24:
                continue
            parsed = _pcap_udp_timestamps(path, {40000, 50000})
            parsed_files += 1
            for port in port_times:
                port_times[port].extend(parsed[port])

        pcm_rx_evidence, pcm_rx_facts = _stream_intervals(
            port_times[40000], source_kind="REAL_PCM_RX_UDP_FROM_DURABLE_PCAP",
            source_id=bootstrap.capture_session_id,
        )
        pcm_tx_evidence, pcm_tx_facts = _stream_intervals(
            port_times[50000], source_kind="REAL_PCM_TX_UDP_FROM_DURABLE_PCAP",
            source_id=bootstrap.capture_session_id,
        )
        pcm_facts = {
            "parsed_durable_pcap_files": parsed_files,
            "pcm_rx": pcm_rx_facts,
            "pcm_tx": pcm_tx_facts,
        }
        fxs_evidence = []
        if fxs_monitor_start and fxs_monitor_end and fxs_monitor_end > fxs_monitor_start:
            fxs_evidence = [EvidenceInterval(
                start=fxs_monitor_start,
                end=fxs_monitor_end,
                interval_type=CoverageIntervalType.COVERED,
                source_kind="REAL_AIM_FXS_MONITOR_AVAILABILITY",
                source_id=bootstrap.capture_session_id,
                certainty="CONFIRMED",
                details={"transcript_path": str(transcript_path)},
            )]

        coverage_result = coordinator.finalize_attempt(
            capture_attempt_id=target_attempt_id,
            call_ref=None,
            channel_evidence={
                "FXS": fxs_evidence,
                "PCM_RX": pcm_rx_evidence,
                "PCM_TX": pcm_tx_evidence,
            },
            channel_applicability={"FXS": True, "PCM_RX": True, "PCM_TX": True},
            signals=[],
            required_channels_for_diagnosis=(),
            independent_support_count=0,
        )
        coverage_window_id = coverage_result.coverage_window_id
    except Exception as exc:
        runtime_errors.append(f"{type(exc).__name__}:{exc}")
    finally:
        transcript_path.write_text("".join(transcript), encoding="utf-8", errors="replace")
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
            if await session.components["reader"].process_matches(
                pid=bootstrap.ownership.producer.pid,
                starttime=bootstrap.ownership.producer.process_starttime,
            ):
                await session.components["producer"].stop_identity(
                    session.token, bootstrap.ownership.producer
                )
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

    track_snapshot: dict[str, Any] = {}
    window_snapshot: dict[str, Any] = {}
    if coverage_window_id:
        with runner.session_factory() as db:
            window = db.get(CoverageWindow, coverage_window_id)
            tracks = list(db.scalars(select(CoverageTrack).where(
                CoverageTrack.coverage_window_id == coverage_window_id
            )))
            for track in tracks:
                interval_count = int(db.scalar(select(func.count(CoverageInterval.id)).where(
                    CoverageInterval.coverage_track_id == track.id
                )) or 0)
                track_snapshot[str(track.channel)] = {
                    "requirement": str(track.requirement),
                    "status": str(track.status),
                    "required_ms": int(track.required_ms or 0),
                    "covered_ms": int(track.covered_ms or 0),
                    "gap_ms": int(track.gap_ms or 0),
                    "unknown_ms": int(track.unknown_ms or 0),
                    "interval_count": interval_count,
                }
            if window is not None:
                window_snapshot = {
                    "id": str(window.id),
                    "status": str(window.status),
                    "required_start_ts": window.required_start_ts.isoformat(),
                    "required_end_ts": window.required_end_ts.isoformat(),
                    "finalized_at": window.finalized_at.isoformat() if window.finalized_at else None,
                }

    required_tracks = ("PCAP", "FXS", "PCM_RX", "PCM_TX")
    all_tracks_complete = all(
        track_snapshot.get(channel, {}).get("status") == "COMPLETE"
        for channel in required_tracks
    )
    all_intervals_persisted = all(
        int(track_snapshot.get(channel, {}).get("interval_count") or 0) > 0
        for channel in required_tracks
    )
    checks = (
        GateCheck("stage1_ready_before_handset", readiness_status == ReadinessStatus.READY,
                  ReadinessStatus.READY.value, readiness_status.value if readiness_status else None),
        GateCheck("stage1_all_real_checks_ready", bool(stage1_checks and all(stage1_checks.as_dict().values())),
                  True, stage1_checks.as_dict() if stage1_checks else None),
        GateCheck("physical_offhook_dtmf_onhook_observed", physical_complete,
                  "OFFHOOK->DTMF->ONHOOK", [x.get("event") for x in observed]),
        GateCheck("real_attempt_bound", target_attempt_id is not None, ">0", target_attempt_id),
        GateCheck("capture_finalizer_durable", bool(finalizer_result and finalizer_result.durable),
                  True, finalizer_result.durable if finalizer_result else None),
        GateCheck("kernel_capture_drops_zero", bool(finalizer_result and finalizer_result.kernel_drops == 0),
                  0, finalizer_result.kernel_drops if finalizer_result else None),
        GateCheck("real_pcm_rx_packets_observed", int(pcm_facts.get("pcm_rx", {}).get("packet_count") or 0) > 0,
                  ">0", pcm_facts.get("pcm_rx", {}).get("packet_count")),
        GateCheck("real_pcm_tx_packets_observed", int(pcm_facts.get("pcm_tx", {}).get("packet_count") or 0) > 0,
                  ">0", pcm_facts.get("pcm_tx", {}).get("packet_count")),
        GateCheck("coverage_window_auto_finalized_complete", window_snapshot.get("status") == "COMPLETE" and bool(window_snapshot.get("finalized_at")),
                  "COMPLETE+finalized_at", window_snapshot),
        GateCheck("coverage_required_tracks_complete", all_tracks_complete,
                  {x: "COMPLETE" for x in required_tracks}, track_snapshot),
        GateCheck("coverage_intervals_persisted", all_intervals_persisted,
                  {x: ">0" for x in required_tracks}, {x: track_snapshot.get(x, {}).get("interval_count") for x in required_tracks}),
        GateCheck("pcm_control_symmetric", pcm_enable_acked == len(pcm_on) and pcm_disable_acked == len(pcm_off),
                  {"on": len(pcm_on), "off": len(pcm_off)}, {"on": pcm_enable_acked, "off": pcm_disable_acked}),
        GateCheck("aim_debug_control_symmetric", debug_enable_acked == len(FULL_DEBUG_ENABLE) and debug_disable_acked == len(FULL_DEBUG_DISABLE),
                  {"on": len(FULL_DEBUG_ENABLE), "off": len(FULL_DEBUG_DISABLE)}, {"on": debug_enable_acked, "off": debug_disable_acked}),
        GateCheck("producer_cleanup_exact_identity", producer_gone, True, producer_gone),
        GateCheck("lease_cleanup_released", lease_released, True, lease_released),
        GateCheck("cleanup_no_exception", not cleanup_errors, [], cleanup_errors),
        GateCheck("runtime_no_exception", not runtime_errors, [], runtime_errors),
    )

    if not physical_complete:
        verdict = GateVerdict.INCONCLUSIVE
    elif runtime_errors and not coverage_window_id:
        verdict = GateVerdict.INCONCLUSIVE
    elif any(check.passed is False for check in checks):
        verdict = GateVerdict.FAIL
    elif any(check.passed is None for check in checks):
        verdict = GateVerdict.INCONCLUSIVE
    else:
        verdict = GateVerdict.PASS

    facts = {
        "release_gate_effect": "R5_REAL_LIVE_COVERAGE_VALIDATION_ONLY_PRODUCTION_V2_DISABLED",
        "capture_session_id": bootstrap.capture_session_id,
        "capture_epoch_id": bootstrap.ownership.capture_epoch_id,
        "lease_epoch": session.token.lease_epoch,
        "voice_context": {
            "gateway_ip": bootstrap.voice_context.gateway_ip,
            "voice_vlan_id": bootstrap.voice_context.voice_vlan_id,
            "interface": bootstrap.voice_context.interface,
        },
        "observed_fxs_events": observed,
        "physical_sequence_complete": physical_complete,
        "capture_attempt_id": target_attempt_id,
        "fxs_monitor_start": fxs_monitor_start.isoformat() if fxs_monitor_start else None,
        "fxs_monitor_end": fxs_monitor_end.isoformat() if fxs_monitor_end else None,
        "pcm": pcm_facts,
        "coverage_window": window_snapshot,
        "coverage_tracks": track_snapshot,
        "finalizer": {
            "durable": finalizer_result.durable if finalizer_result else None,
            "kernel_drops": finalizer_result.kernel_drops if finalizer_result else None,
            "sealed": finalizer_result.final_segments_sealed if finalizer_result else None,
            "transferred": finalizer_result.final_segments_transferred if finalizer_result else None,
            "acked": finalizer_result.acknowledged if finalizer_result else None,
            "remote_deleted": finalizer_result.remote_deleted if finalizer_result else None,
        },
        "runtime_errors": runtime_errors,
        "cleanup_errors": cleanup_errors,
        "transcript_path": str(transcript_path),
    }
    await GateEvidenceCollector(
        session_factory=runner.session_factory,
        adapter=runner.adapter,
        object_root=runner.object_root,
        repo_root=runner.repo_root,
    ).collect(
        paths=paths,
        gate_id=gate_id,
        capture_session_id=bootstrap.capture_session_id,
        device_id=str(device.id),
        facts=facts,
    )
    return GateCaseResult(
        gate_id=gate_id,
        verdict=verdict,
        checks=checks,
        summary="R5 real call -> durable evidence -> automatic Coverage Ledger COMPLETE",
        evidence_bundle=str(paths.case_dir),
        facts=facts,
    )
