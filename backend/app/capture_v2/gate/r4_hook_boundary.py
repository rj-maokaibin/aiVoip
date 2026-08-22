from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.capture_v2.c_bridge import CaptureV2CBridge
from app.capture_v2.d_bridge import CaptureV2DSession
from app.capture_v2.db_models import CaptureAttempt, CaptureEvent
from app.capture_v2.fxs.sanitizer import RawFxsEvent
from app.capture_v2.gate.evidence import GateEvidenceCollector
from app.capture_v2.gate.models import GateCaseResult, GateCheck, GateRunPaths, GateVerdict
from app.reproduction.fxs_event_monitor import FxsEventMonitor, FULL_DEBUG_DISABLE, FULL_DEBUG_ENABLE


def _parse_device_source_ts(timestamp: str, offset: str) -> datetime:
    return datetime.strptime(f"{timestamp} {offset}", "%Y-%m-%d %H:%M:%S.%f %z")


def _load_rows(session_factory, capture_session_id: str):
    with session_factory() as db:
        attempts = list(db.scalars(select(CaptureAttempt).where(
            CaptureAttempt.capture_session_id == capture_session_id
        ).order_by(CaptureAttempt.attempt_no)))
        events = list(db.scalars(select(CaptureEvent).where(
            CaptureEvent.capture_session_id == capture_session_id
        ).order_by(CaptureEvent.source_ts)))
    return attempts, events


def _event_payload(event: CaptureEvent) -> dict:
    return dict(event.payload or {})


def _pulse_candidates(observed: list[dict[str, Any]], *, after_first_dtmf: bool) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    armed = not after_first_dtmf
    pending_onhook: dict[str, Any] | None = None
    for row in observed:
        kind = str(row.get("event") or "").upper()
        if kind == "DTMF":
            armed = True
        if not armed:
            continue
        if kind == "ONHOOK" and pending_onhook is None:
            pending_onhook = row
            continue
        if kind == "OFFHOOK" and pending_onhook is not None:
            start = datetime.fromisoformat(str(pending_onhook["source_ts"]))
            end = datetime.fromisoformat(str(row["source_ts"]))
            ms = int((end - start).total_seconds() * 1000)
            candidates.append({
                "onhook_source_ts": start.isoformat(),
                "offhook_source_ts": end.isoformat(),
                "duration_ms": ms,
            })
            pending_onhook = None
    return candidates


async def _setup(runner, *, reproduction_session_id: str, device: Any, worker_id: str,
                 transport: str, gate_id: str):
    c_session = await CaptureV2CBridge(
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
    d_session = CaptureV2DSession(
        capture_session_id=c_session.bootstrap.capture_session_id,
        session_factory=runner.session_factory,
        effective_profile=c_session.bootstrap.effective_profile.model_dump(),
    )
    paths = GateRunPaths.create(runner.output_root, gate_id, str(device.id))
    return c_session, d_session, paths


async def run_r4_real_hook_flash(
    runner, *, reproduction_session_id: str, device: Any, worker_id: str,
    gate_id: str, duration_seconds: float = 300.0, transport: str = "scp",
) -> GateCaseResult:
    """Real DUT Hook Flash gate.

    Operator sequence: OFFHOOK -> DTMF -> valid 100..1000ms ONHOOK pulse -> OFFHOOK
    -> DTMF -> final ONHOOK. Only real AIM/FXS source events are consumed.
    An out-of-window physical pulse is INCONCLUSIVE, never product FAIL.
    """
    duration_seconds = float(duration_seconds)
    if duration_seconds <= 0 or duration_seconds > 300:
        raise ValueError("R4_FXS_DURATION_OUT_OF_RANGE")

    c_session, d_session, paths = await _setup(
        runner, reproduction_session_id=reproduction_session_id, device=device,
        worker_id=worker_id, transport=transport, gate_id=gate_id,
    )
    bootstrap = c_session.bootstrap
    transcript_path = paths.dut_dir / "aim_fxs_transcript.log"
    tz_result = await runner.adapter.execute_shell("date +%z", retries=0)
    device_offset = (str(tz_result.stdout or "").strip().splitlines()[-1:] or [""])[0].strip()
    timezone_proven = len(device_offset) == 5 and device_offset[0] in "+-" and device_offset[1:].isdigit()
    if not timezone_proven:
        device_offset = "+0000"

    monitor = FxsEventMonitor(read_aim_chunk=lambda: None, write_aim=lambda _: None)
    monitor.start(enable_debug=False)
    observed: list[dict[str, Any]] = []
    transcript: list[str] = []
    debug_enable_acked = 0
    debug_disable_acked = 0
    monitor_error: str | None = None
    call_active = False
    first_dtmf_seen = False
    semantic_flash_seen = False
    post_flash_dtmf_seen = False
    final_onhook_ts: datetime | None = None
    final_onhook_wall: float | None = None

    await runner.adapter.ensure_aim_session_ready()
    try:
        for command in FULL_DEBUG_ENABLE:
            await runner.adapter.execute_cli(command)
            debug_enable_acked += 1
        async with c_session:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + duration_seconds
            while loop.time() < deadline:
                if final_onhook_ts is not None and final_onhook_wall is not None and loop.time() - final_onhook_wall > 1.10:
                    d_session.flush_pending_onhook(now=final_onhook_ts + timedelta(milliseconds=1101))
                    _, current_events = _load_rows(runner.session_factory, bootstrap.capture_session_id)
                    if any(e.event_type == "ATTEMPT_ENDED" and e.source_ts == final_onhook_ts for e in current_events):
                        break
                remaining = max(0.05, deadline - loop.time())
                chunk = await runner.adapter.read_aim_chunk(timeout=min(0.5, remaining))
                if not chunk:
                    await asyncio.sleep(0.03)
                    continue
                transcript.append(chunk)
                for event in monitor.feed(chunk):
                    source_ts = _parse_device_source_ts(event.timestamp, device_offset)
                    d_session.ingest_fxs(
                        RawFxsEvent(source_ts=source_ts, event=event.event, digit=event.digit, line=event.line),
                        call_active=call_active,
                    )
                    row = {
                        "timestamp": event.timestamp, "source_ts": source_ts.isoformat(),
                        "line": event.line, "event": event.event, "digit": event.digit,
                    }
                    observed.append(row)
                    if str(event.event).upper() == "DTMF" and not first_dtmf_seen:
                        first_dtmf_seen = True
                        call_active = True
                    _, current_events = _load_rows(runner.session_factory, bootstrap.capture_session_id)
                    if any(e.event_type == "FXS_HOOK_FLASH" for e in current_events):
                        semantic_flash_seen = True
                    if semantic_flash_seen and str(event.event).upper() == "DTMF" and first_dtmf_seen:
                        post_flash_dtmf_seen = True
                    if semantic_flash_seen and post_flash_dtmf_seen and str(event.event).upper() == "ONHOOK" and final_onhook_ts is None:
                        final_onhook_ts = source_ts
                        final_onhook_wall = loop.time()
    except Exception as exc:
        monitor_error = f"{type(exc).__name__}:{exc}"
    finally:
        transcript_path.write_text("".join(transcript), encoding="utf-8", errors="replace")
        for command in FULL_DEBUG_DISABLE:
            try:
                await runner.adapter.execute_cli(command)
                debug_disable_acked += 1
            except Exception:
                pass

    attempts, events = _load_rows(runner.session_factory, bootstrap.capture_session_id)
    flashes = [e for e in events if e.event_type == "FXS_HOOK_FLASH"]
    glitches = [e for e in events if e.event_type == "FXS_HOOK_GLITCH"]
    ended = [e for e in events if e.event_type == "ATTEMPT_ENDED"]
    dtmfs = [e for e in events if e.event_type == "FXS_DTMF"]
    candidates = _pulse_candidates(observed, after_first_dtmf=True)
    valid_candidates = [x for x in candidates if 100 <= int(x["duration_ms"]) <= 1000]
    valid_physical_pulse = bool(valid_candidates)
    flash_durations = [int(_event_payload(e).get("duration_ms", -1)) for e in flashes]
    flash_attempt_ids = {e.entity_id for e in flashes if e.entity_id}
    dtmf_attempt_ids = {e.entity_id for e in dtmfs if e.entity_id}
    ended_attempt_ids = {e.entity_id for e in ended if e.entity_id}
    same_attempt = bool(flash_attempt_ids) and flash_attempt_ids.issubset(dtmf_attempt_ids) and flash_attempt_ids.issubset(ended_attempt_ids)
    normal_ended = [a for a in attempts if str(a.state) == "ENDED" and str(a.classification) == "NORMAL"]

    checks = (
        GateCheck("full_debug_enable_prompt_acked", debug_enable_acked == len(FULL_DEBUG_ENABLE), len(FULL_DEBUG_ENABLE), debug_enable_acked),
        GateCheck("full_debug_cleanup_prompt_acked", debug_disable_acked == len(FULL_DEBUG_DISABLE), len(FULL_DEBUG_DISABLE), debug_disable_acked),
        GateCheck("device_source_timezone_observed", timezone_proven, "DUT date +%z", device_offset),
        GateCheck("physical_hook_flash_pulse_in_window", valid_physical_pulse, "100..1000ms", candidates),
        GateCheck("semantic_hook_flash_recorded", bool(flashes), ">0 FXS_HOOK_FLASH", len(flashes)),
        GateCheck("semantic_hook_flash_duration_in_window", bool(flash_durations) and all(100 <= x <= 1000 for x in flash_durations), "100..1000ms", flash_durations),
        GateCheck("hook_flash_not_glitch", not glitches, 0, len(glitches)),
        GateCheck("same_attempt_across_flash", same_attempt, True, {"flash": sorted(flash_attempt_ids), "dtmf": sorted(dtmf_attempt_ids), "ended": sorted(ended_attempt_ids)}),
        GateCheck("no_attempt_split", len(attempts) == 1, 1, len(attempts)),
        GateCheck("post_flash_business_evidence_observed", post_flash_dtmf_seen, True, post_flash_dtmf_seen),
        GateCheck("final_onhook_source_time_preserved", final_onhook_ts is not None and any(e.source_ts == final_onhook_ts for e in ended), final_onhook_ts.isoformat() if final_onhook_ts else None, [e.source_ts.isoformat() for e in ended]),
        GateCheck("normal_attempt_ended", len(normal_ended) == 1, 1, len(normal_ended)),
        GateCheck("monitor_runtime_no_exception", monitor_error is None, None, monitor_error),
    )
    if not valid_physical_pulse:
        verdict = GateVerdict.INCONCLUSIVE
    elif any(c.passed is False for c in checks):
        verdict = GateVerdict.FAIL
    elif any(c.passed is None for c in checks):
        verdict = GateVerdict.INCONCLUSIVE
    else:
        verdict = GateVerdict.PASS

    facts = {
        "capture_session_id": bootstrap.capture_session_id,
        "capture_epoch_id": bootstrap.ownership.capture_epoch_id,
        "lease_epoch": c_session.token.lease_epoch,
        "device_timezone_offset": device_offset,
        "observed_fxs_events": observed,
        "hook_pulse_candidates": candidates,
        "valid_hook_flash_candidates": valid_candidates,
        "semantic_hook_flash_count": len(flashes),
        "semantic_hook_flash_durations_ms": flash_durations,
        "semantic_glitch_count": len(glitches),
        "capture_attempt_count": len(attempts),
        "post_flash_dtmf_seen": post_flash_dtmf_seen,
        "final_onhook_source_ts": final_onhook_ts.isoformat() if final_onhook_ts else None,
        "monitor_error": monitor_error,
        "transcript_path": str(transcript_path),
    }
    await GateEvidenceCollector(
        session_factory=runner.session_factory, adapter=runner.adapter,
        object_root=runner.object_root, repo_root=runner.repo_root,
    ).collect(paths=paths, gate_id=gate_id, capture_session_id=bootstrap.capture_session_id,
              device_id=str(device.id), facts=facts)
    return GateCaseResult(
        gate_id=gate_id, verdict=verdict, checks=checks,
        summary="R4 real Hook Flash source-time / same-Attempt boundary",
        evidence_bundle=str(paths.case_dir), facts=facts,
    )


async def run_r4_real_post_onhook_rebound(
    runner, *, reproduction_session_id: str, device: Any, worker_id: str,
    gate_id: str, duration_seconds: float = 300.0, transport: str = "scp",
) -> GateCaseResult:
    """Real DUT post-hangup rebound calibration gate.

    Operator makes a normal call/end, then creates or allows a real short OFFHOOK->ONHOOK
    rebound within 500ms. A <=100ms pulse must classify as FXS_HOOK_GLITCH and must
    not create a second confirmed/normal business Attempt.
    """
    duration_seconds = float(duration_seconds)
    if duration_seconds <= 0 or duration_seconds > 300:
        raise ValueError("R4_FXS_DURATION_OUT_OF_RANGE")

    c_session, d_session, paths = await _setup(
        runner, reproduction_session_id=reproduction_session_id, device=device,
        worker_id=worker_id, transport=transport, gate_id=gate_id,
    )
    bootstrap = c_session.bootstrap
    transcript_path = paths.dut_dir / "aim_fxs_transcript.log"
    tz_result = await runner.adapter.execute_shell("date +%z", retries=0)
    device_offset = (str(tz_result.stdout or "").strip().splitlines()[-1:] or [""])[0].strip()
    timezone_proven = len(device_offset) == 5 and device_offset[0] in "+-" and device_offset[1:].isdigit()
    if not timezone_proven:
        device_offset = "+0000"

    monitor = FxsEventMonitor(read_aim_chunk=lambda: None, write_aim=lambda _: None)
    monitor.start(enable_debug=False)
    observed: list[dict[str, Any]] = []
    transcript: list[str] = []
    debug_enable_acked = 0
    debug_disable_acked = 0
    monitor_error: str | None = None

    await runner.adapter.ensure_aim_session_ready()
    try:
        for command in FULL_DEBUG_ENABLE:
            await runner.adapter.execute_cli(command)
            debug_enable_acked += 1
        async with c_session:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + duration_seconds
            while loop.time() < deadline:
                remaining = max(0.05, deadline - loop.time())
                chunk = await runner.adapter.read_aim_chunk(timeout=min(0.5, remaining))
                if not chunk:
                    await asyncio.sleep(0.03)
                    continue
                transcript.append(chunk)
                for event in monitor.feed(chunk):
                    source_ts = _parse_device_source_ts(event.timestamp, device_offset)
                    d_session.ingest_fxs(RawFxsEvent(
                        source_ts=source_ts, event=event.event, digit=event.digit, line=event.line,
                    ))
                    observed.append({
                        "timestamp": event.timestamp, "source_ts": source_ts.isoformat(),
                        "line": event.line, "event": event.event, "digit": event.digit,
                    })
                _, current_events = _load_rows(runner.session_factory, bootstrap.capture_session_id)
                if any(e.event_type == "FXS_HOOK_GLITCH" for e in current_events):
                    break
    except Exception as exc:
        monitor_error = f"{type(exc).__name__}:{exc}"
    finally:
        transcript_path.write_text("".join(transcript), encoding="utf-8", errors="replace")
        for command in FULL_DEBUG_DISABLE:
            try:
                await runner.adapter.execute_cli(command)
                debug_disable_acked += 1
            except Exception:
                pass

    attempts, events = _load_rows(runner.session_factory, bootstrap.capture_session_id)
    glitches = [e for e in events if e.event_type == "FXS_HOOK_GLITCH"]
    normal_ended = [a for a in attempts if str(a.state) == "ENDED" and str(a.classification) == "NORMAL"]
    business_attempts = [a for a in attempts if str(a.classification) != "FXS_HOOK_GLITCH"]
    candidates: list[dict[str, Any]] = []
    raw = observed
    # Find post-hangup OFFHOOK->ONHOOK pulses and require OFFHOOK to start <=500ms after a prior ONHOOK.
    for i in range(len(raw) - 2):
        if str(raw[i].get("event")).upper() != "ONHOOK":
            continue
        base_onhook = datetime.fromisoformat(str(raw[i]["source_ts"]))
        for j in range(i + 1, min(len(raw), i + 5)):
            if str(raw[j].get("event")).upper() != "OFFHOOK":
                continue
            rebound_start = datetime.fromisoformat(str(raw[j]["source_ts"]))
            start_delay_ms = int((rebound_start - base_onhook).total_seconds() * 1000)
            if start_delay_ms < 0 or start_delay_ms > 500:
                continue
            for k in range(j + 1, min(len(raw), j + 5)):
                if str(raw[k].get("event")).upper() != "ONHOOK":
                    continue
                rebound_end = datetime.fromisoformat(str(raw[k]["source_ts"]))
                duration_ms = int((rebound_end - rebound_start).total_seconds() * 1000)
                candidates.append({
                    "base_onhook_source_ts": base_onhook.isoformat(),
                    "rebound_offhook_source_ts": rebound_start.isoformat(),
                    "rebound_onhook_source_ts": rebound_end.isoformat(),
                    "start_delay_ms": start_delay_ms,
                    "duration_ms": duration_ms,
                })
                break
            break
    valid_candidates = [x for x in candidates if 0 <= x["start_delay_ms"] <= 500 and 0 <= x["duration_ms"] <= 100]
    glitch_durations = [int(_event_payload(e).get("duration_ms", -1)) for e in glitches]

    checks = (
        GateCheck("full_debug_enable_prompt_acked", debug_enable_acked == len(FULL_DEBUG_ENABLE), len(FULL_DEBUG_ENABLE), debug_enable_acked),
        GateCheck("full_debug_cleanup_prompt_acked", debug_disable_acked == len(FULL_DEBUG_DISABLE), len(FULL_DEBUG_DISABLE), debug_disable_acked),
        GateCheck("device_source_timezone_observed", timezone_proven, "DUT date +%z", device_offset),
        GateCheck("physical_post_onhook_rebound_observed", bool(valid_candidates), {"start_delay_ms": "0..500", "duration_ms": "0..100"}, candidates),
        GateCheck("semantic_hook_glitch_recorded", bool(glitches), ">0 FXS_HOOK_GLITCH", len(glitches)),
        GateCheck("semantic_glitch_duration_le_100ms", bool(glitch_durations) and all(0 <= x <= 100 for x in glitch_durations), "<=100ms", glitch_durations),
        GateCheck("original_normal_attempt_ended", len(normal_ended) >= 1, ">=1", len(normal_ended)),
        GateCheck("no_second_confirmed_normal_business_attempt", len(business_attempts) == 1, 1, len(business_attempts)),
        GateCheck("monitor_runtime_no_exception", monitor_error is None, None, monitor_error),
    )
    if not valid_candidates:
        verdict = GateVerdict.INCONCLUSIVE
    elif any(c.passed is False for c in checks):
        verdict = GateVerdict.FAIL
    elif any(c.passed is None for c in checks):
        verdict = GateVerdict.INCONCLUSIVE
    else:
        verdict = GateVerdict.PASS

    facts = {
        "capture_session_id": bootstrap.capture_session_id,
        "capture_epoch_id": bootstrap.ownership.capture_epoch_id,
        "lease_epoch": c_session.token.lease_epoch,
        "device_timezone_offset": device_offset,
        "observed_fxs_events": observed,
        "post_onhook_rebound_candidates": candidates,
        "valid_rebound_candidates": valid_candidates,
        "semantic_hook_glitch_count": len(glitches),
        "semantic_hook_glitch_durations_ms": glitch_durations,
        "capture_attempt_count_total": len(attempts),
        "business_attempt_count_excluding_glitch": len(business_attempts),
        "normal_ended_attempt_count": len(normal_ended),
        "monitor_error": monitor_error,
        "transcript_path": str(transcript_path),
    }
    await GateEvidenceCollector(
        session_factory=runner.session_factory, adapter=runner.adapter,
        object_root=runner.object_root, repo_root=runner.repo_root,
    ).collect(paths=paths, gate_id=gate_id, capture_session_id=bootstrap.capture_session_id,
              device_id=str(device.id), facts=facts)
    return GateCaseResult(
        gate_id=gate_id, verdict=verdict, checks=checks,
        summary="R4 real post-ONHOOK rebound / FXS_HOOK_GLITCH calibration",
        evidence_bundle=str(paths.case_dir), facts=facts,
    )
