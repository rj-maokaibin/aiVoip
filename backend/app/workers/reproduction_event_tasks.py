from __future__ import annotations

import asyncio
import time

from celery.utils.log import get_task_logger

from app.contracts.enums import ReproductionState
from app.core.config import settings
from app.db.models import CaseDevice, ReproductionSession
from app.db.session import SessionLocal
from app.integrations.credentials import get_credential_provider, LocalSecretCredentialProvider
from app.reproduction.fxs_event_monitor import FxsEventMonitor
from app.workers.celery_app import celery_app

log = get_task_logger(__name__)

# States during which the monitor should keep listening for FXS activity / call
# capture. The real-mode watcher also advances through CALL_DETECTED/CAPTURING
# while a media-bound call is being captured.
_LISTENING_STATES = {
    ReproductionState.WATCHING.value,
    ReproductionState.ACTIVITY_DETECTED.value,
    ReproductionState.CALL_DETECTED.value,
    ReproductionState.CAPTURING.value,
}


def _session_listening(db, session_id: str) -> ReproductionSession | None:
    row = db.get(ReproductionSession, session_id)
    if row is None:
        return None
    return row


async def _watch(session_id: str, *, max_seconds: int = 900) -> dict:
    db = SessionLocal()
    try:
        session = _session_listening(db, session_id)
        if session is None:
            return {'status': 'SESSION_NOT_FOUND', 'session_id': session_id}
        device = db.get(CaseDevice, session.device_id)
        if device is None:
            return {'status': 'DEVICE_NOT_FOUND', 'session_id': session_id}

        from app.reproduction.platform_factory import build_orchestrator, resolve_platform_mode
        if resolve_platform_mode() == 'mock':
            return await _watch_mock(db, session, device, max_seconds)
        return await _watch_real(db, session, device, max_seconds)
    finally:
        db.close()


async def _watch_mock(db, session, device, max_seconds: int) -> dict:
    """Mock-mode watcher: no device I/O; the mock platform reports no FXS events."""
    orch, _close = build_orchestrator(adapter=None, connect=False)
    events_handled = 0
    started = time.monotonic()
    try:
        while time.monotonic() - started < max_seconds:
            row = _session_listening(db, session.id)
            if row is None or ReproductionState(row.state) not in _LISTENING_STATES:
                break
            await asyncio.sleep(0.5)
    finally:
        _close()
    return {'status': 'DONE', 'session_id': session.id, 'events_handled': events_handled,
            'state': _session_listening(db, session.id).state if _session_listening(db, session.id) else 'GONE'}


async def _watch_real(db, session, device, max_seconds: int) -> dict:
    """Real-mode watcher: FXS events + real CALL analysis through the real platform.

    The platform owns the AsyncSSHDeviceAdapter on its dedicated bridge loop, so all
    asyncssh I/O (connect, AIM commands, raw AIM stream reads, PCM probes) share ONE
    event loop. A background reader pushes raw AIM chunks into a thread-safe queue
    and the synchronous orchestrator polls it — no cross-loop handoff, no deadlock.

    CALL-level analysis (the previously unconnected link) is driven by the real
    platform's media-binding signal: a call is bound when the PCM mirror stream
    (UDP 40000/50000) becomes active after an FXS OFFHOOK attempt, and ended when
    the FXS ONHOOK arrives while a call is bound. No AIM SIP plaintext is needed
    (verified: `de sip de` does not emit INVITE/BYE on the PTY).
    """
    provider = get_credential_provider()
    password = await provider.get_password(sn=device.sn, ip=device.ip)
    username = device.username
    if isinstance(provider, LocalSecretCredentialProvider):
        try:
            username = provider.resolve_username(ip=device.ip, fallback=username)
        except Exception:
            pass

    from app.collectors.asyncssh_adapter import AsyncSSHDeviceAdapter
    from app.reproduction.quick import QuickAnalysisInput
    from app.contracts.enums import CallVerdict
    adapter = AsyncSSHDeviceAdapter(ip=device.ip, port=device.ssh_port, username=username, password=password)
    orch, _close = build_orchestrator(adapter=adapter, connect=True)
    try:
        # Start the bridge-loop AIM reader and wire its monitor into the orchestrator.
        orch.fxs_event_monitor = orch.platform.start_fxs_monitor()
        events_handled = 0
        calls_bound = 0
        calls_ended = 0
        active_call_id: str | None = None
        last_media_probe = 0.0
        media_probe_interval = 3.0
        last_media_capture = 0.0
        media_capture_interval = 4.0
        started = time.monotonic()
        try:
            while time.monotonic() - started < max_seconds:
                row = _session_listening(db, session.id)
                if row is None:
                    break
                state = ReproductionState(row.state)
                # Continue listening only in the watching/activity/call states; stop
                # when the session left for capture/analysis/cleanup or finished.
                if state in {ReproductionState.WATCHING, ReproductionState.ACTIVITY_DETECTED,
                             ReproductionState.CALL_DETECTED, ReproductionState.CAPTURING}:
                    pass
                else:
                    break
                ctx = orch.platform.resolve_voice_context(device) if hasattr(orch.platform, 'resolve_voice_context') else None

                # 1. Poll FXS events (OFFHOOK -> record_activity; ONHOOK with no bound
                #    call -> end_activity_without_call inside record_fxs_event).
                for ev in orch.fxs_event_monitor.poll():
                    handled = orch.record_fxs_event(db, session=row, event=ev, actor='reproduction-worker')
                    if handled is not None:
                        events_handled += 1
                    db.commit()
                    # FXS ONHOOK while a call is bound -> REAL end_call (CALL_QUICK
                    # analysis on the real capture). record_fxs_event does not consume
                    # ONHOOK in CALL_DETECTED/CAPTURING, so we handle it here.
                    if ev.event == 'ONHOOK' and active_call_id is not None:
                        now_state = ReproductionState(_session_listening(db, session.id).state)
                        if now_state in {ReproductionState.CALL_DETECTED, ReproductionState.CAPTURING}:
                            rel = orch.fxs_event_monitor.relative_ms()
                            call, _decision = orch.end_call(
                                db, session=row, call_id=active_call_id, relative_ms=rel,
                                signal=QuickAnalysisInput(verdict=CallVerdict.INCONCLUSIVE, findings=()),
                                end_anchor='FXS_ONHOOK', actor='reproduction-worker',
                            )
                            active_call_id = None
                            calls_ended += 1
                            db.commit()

                # 2. Periodic media probe: if an FXS attempt is active (ACTIVITY_DETECTED)
                #    and no call is bound yet, bind on the PCM mirror stream becoming live.
                now = time.monotonic()
                if (now - last_media_probe) >= media_probe_interval:
                    last_media_probe = now
                    cur_state = ReproductionState(_session_listening(db, session.id).state)
                    if (cur_state == ReproductionState.ACTIVITY_DETECTED and active_call_id is None
                            and orch.platform.pcm_media_active(context=ctx)):
                        rel = orch.fxs_event_monitor.relative_ms()
                        call = orch.bind_call(
                            db, session=row, relative_ms=rel,
                            external_call_ref=orch.platform.media_binding_call_ref(),
                            binding_event='RTP_STREAM_START', actor='reproduction-worker',
                        )
                        active_call_id = call.id
                        calls_bound += 1
                        db.commit()

                # 3. Periodic media accumulation during the conversation: while a call is
                #    bound and still capturing, keep appending short PCM segments so the
                #    merged capture spans the whole call (not just the bind_call window).
                if active_call_id is not None and (now - last_media_capture) >= media_capture_interval:
                    last_media_capture = now
                    cur_state = ReproductionState(_session_listening(db, session.id).state)
                    if cur_state in {ReproductionState.CAPTURING, ReproductionState.CALL_DETECTED}:
                        rel = orch.fxs_event_monitor.relative_ms()
                        orch.platform.build_live_probe(
                            context=ctx, start_ms=int(rel), call_id=active_call_id,
                        )
                        db.commit()

                if events_handled == 0:
                    await asyncio.sleep(0.2)
        finally:
            try:
                orch.platform.stop_fxs_monitor()
            except Exception:
                pass
        return {'status': 'DONE', 'session_id': session.id, 'events_handled': events_handled,
                'calls_bound': calls_bound, 'calls_ended': calls_ended,
                'state': _session_listening(db, session.id).state if _session_listening(db, session.id) else 'GONE'}
    finally:
        _close()


@celery_app.task(name='reproduction.watch_fxs_events', bind=True, autoretry_for=(), max_retries=0)
def watch_fxs_events(self, session_id: str, max_seconds: int = 900):
    """Watch a reproduction session's DUT for FXS activity and feed it to the
    orchestrator as real activity anchors. Runs until the session leaves the
    watching/activity-detected states or the timeout elapses.

    After the watcher finishes (session reached a terminal state — call captured
    and cleanup verified, or the watch window elapsed), hand the session to the
    diagnosis worker automatically, closing the "reproduction done but diagnosis
    must be clicked by hand" gap.
    """
    result = asyncio.run(_watch(session_id, max_seconds=max_seconds))
    from app.workers.reproduction_tasks import ensure_reproduction_diagnosis
    diag = ensure_reproduction_diagnosis(session_id)
    result['diagnosis'] = diag
    return result
