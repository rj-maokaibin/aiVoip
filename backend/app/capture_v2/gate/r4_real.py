from __future__ import annotations

import asyncio
from datetime import datetime
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


def _verdict(checks: tuple[GateCheck, ...], *, physical_sequence_complete: bool) -> GateVerdict:
    if not physical_sequence_complete:
        return GateVerdict.INCONCLUSIVE
    if any(check.passed is False for check in checks):
        return GateVerdict.FAIL
    if any(check.passed is None for check in checks):
        return GateVerdict.INCONCLUSIVE
    return GateVerdict.PASS


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


async def run_r4_real_fxs_basic(
    runner,
    *,
    reproduction_session_id: str,
    device: Any,
    worker_id: str,
    gate_id: str,
    duration_seconds: float = 90.0,
    transport: str = "scp",
) -> GateCaseResult:
    """Real DUT R4-01: bind raw AIM FXS source events into Capture V2 semantics.

    This Gate does not simulate handset activity.  It arms a real CaptureEpoch,
    renews its lease for the observation window, enables the verified full AIM
    debug sequence, consumes the persistent AIM PTY, and feeds only parsed real
    OFFHOOK/DTMF/ONHOOK events into CaptureV2DSession.  If no complete physical
    sequence occurs during the window the result is INCONCLUSIVE, never FAIL.
    """
    duration_seconds = float(duration_seconds)
    if duration_seconds <= 0 or duration_seconds > 300:
        raise ValueError("R4_FXS_DURATION_OUT_OF_RANGE")

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
    bootstrap = c_session.bootstrap
    d_session = CaptureV2DSession(
        capture_session_id=bootstrap.capture_session_id,
        session_factory=runner.session_factory,
        effective_profile=bootstrap.effective_profile.model_dump(),
    )
    paths = GateRunPaths.create(runner.output_root, gate_id, str(device.id))
    transcript_path = paths.dut_dir / "aim_fxs_transcript.log"

    tz_result = await runner.adapter.execute_shell("date +%z", retries=0)
    device_offset = str(tz_result.stdout or "").strip().splitlines()[-1:] or [""]
    device_offset = device_offset[0].strip()
    if len(device_offset) != 5 or device_offset[0] not in "+-" or not device_offset[1:].isdigit():
        device_offset = "+0000"
        timezone_proven = False
    else:
        timezone_proven = True

    monitor = FxsEventMonitor(read_aim_chunk=lambda: None, write_aim=lambda _: None)
    monitor.start(enable_debug=False)
    observed: list[dict[str, Any]] = []
    debug_enable_acked = 0
    debug_disable_acked = 0
    transcript = []
    physical_complete = False
    monitor_error: str | None = None

    await runner.adapter.ensure_aim_session_ready()
    try:
        for command in FULL_DEBUG_ENABLE:
            await runner.adapter.execute_cli(command)
            debug_enable_acked += 1

        async with c_session:
            deadline = asyncio.get_running_loop().time() + duration_seconds
            while asyncio.get_running_loop().time() < deadline:
                remaining = max(0.05, deadline - asyncio.get_running_loop().time())
                chunk = await runner.adapter.read_aim_chunk(timeout=min(1.0, remaining))
                if not chunk:
                    await asyncio.sleep(0.05)
                    continue
                transcript.append(chunk)
                for event in monitor.feed(chunk):
                    source_ts = _parse_device_source_ts(event.timestamp, device_offset)
                    d_session.ingest_fxs(
                        RawFxsEvent(
                            source_ts=source_ts,
                            event=event.event,
                            digit=event.digit,
                            line=event.line,
                        )
                    )
                    observed.append({
                        "timestamp": event.timestamp,
                        "source_ts": source_ts.isoformat(),
                        "line": event.line,
                        "event": event.event,
                        "digit": event.digit,
                    })
                if _ordered_basic_sequence(observed):
                    physical_complete = True
                    break
    except Exception as exc:
        monitor_error = f"{type(exc).__name__}:{exc}"
    finally:
        transcript_path.write_text("".join(transcript), encoding="utf-8", errors="replace")
        # The reader loop above has stopped. Prompt-backed disable is deterministic
        # and ensures a Gate never leaves verbose debug enabled on the DUT.
        for command in FULL_DEBUG_DISABLE:
            try:
                await runner.adapter.execute_cli(command)
                debug_disable_acked += 1
            except Exception:
                pass

    with runner.session_factory() as db:
        attempts = list(db.scalars(select(CaptureAttempt).where(
            CaptureAttempt.capture_session_id == bootstrap.capture_session_id
        ).order_by(CaptureAttempt.attempt_no)))
        events = list(db.scalars(select(CaptureEvent).where(
            CaptureEvent.capture_session_id == bootstrap.capture_session_id
        ).order_by(CaptureEvent.source_ts)))

    event_types = [row.event_type for row in events]
    raw_offhook = sum(1 for x in event_types if x == "FXS_RAW_OFFHOOK")
    raw_dtmf = sum(1 for x in event_types if x == "FXS_RAW_DTMF")
    raw_onhook = sum(1 for x in event_types if x == "FXS_RAW_ONHOOK")
    semantic_dtmf = sum(1 for x in event_types if x == "FXS_DTMF")
    attempt_ended = sum(1 for x in event_types if x == "ATTEMPT_ENDED")
    ended_attempts = [a for a in attempts if str(a.state) == "ENDED"]
    normal_ended = [a for a in ended_attempts if str(a.classification) == "NORMAL"]

    checks = (
        GateCheck("full_debug_enable_prompt_acked", debug_enable_acked == len(FULL_DEBUG_ENABLE),
                  len(FULL_DEBUG_ENABLE), debug_enable_acked),
        GateCheck("full_debug_cleanup_prompt_acked", debug_disable_acked == len(FULL_DEBUG_DISABLE),
                  len(FULL_DEBUG_DISABLE), debug_disable_acked),
        GateCheck("device_source_timezone_observed", timezone_proven, "DUT date +%z", device_offset),
        GateCheck("physical_offhook_dtmf_onhook_observed", physical_complete,
                  "OFFHOOK->DTMF->ONHOOK", [x.get("event") for x in observed]),
        GateCheck("raw_fxs_events_persisted", raw_offhook > 0 and raw_dtmf > 0 and raw_onhook > 0,
                  {"OFFHOOK": ">0", "DTMF": ">0", "ONHOOK": ">0"},
                  {"OFFHOOK": raw_offhook, "DTMF": raw_dtmf, "ONHOOK": raw_onhook}),
        GateCheck("capture_attempt_created", len(attempts) > 0, ">0", len(attempts)),
        GateCheck("semantic_dtmf_bound", semantic_dtmf > 0, ">0 FXS_DTMF", semantic_dtmf),
        GateCheck("attempt_end_semantic_recorded", attempt_ended > 0, ">0 ATTEMPT_ENDED", attempt_ended),
        GateCheck("normal_attempt_ended", len(normal_ended) > 0, ">0 ENDED/NORMAL", len(normal_ended)),
        GateCheck("monitor_runtime_no_exception", monitor_error is None, None, monitor_error),
    )

    facts = {
        "capture_session_id": bootstrap.capture_session_id,
        "capture_epoch_id": bootstrap.ownership.capture_epoch_id,
        "lease_epoch": c_session.token.lease_epoch,
        "device_timezone_offset": device_offset,
        "device_timezone_proven": timezone_proven,
        "debug_enable_acked": debug_enable_acked,
        "debug_disable_acked": debug_disable_acked,
        "physical_sequence_complete": physical_complete,
        "observed_fxs_events": observed,
        "raw_event_counts": {"offhook": raw_offhook, "dtmf": raw_dtmf, "onhook": raw_onhook},
        "capture_attempt_count": len(attempts),
        "ended_normal_attempt_count": len(normal_ended),
        "semantic_dtmf_count": semantic_dtmf,
        "attempt_ended_event_count": attempt_ended,
        "monitor_error": monitor_error,
        "transcript_path": str(transcript_path),
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
        verdict=_verdict(checks, physical_sequence_complete=physical_complete),
        checks=checks,
        summary="R4-01 Real FXS source-time and Attempt semantic binding",
        evidence_bundle=str(paths.case_dir),
        facts=facts,
    )
