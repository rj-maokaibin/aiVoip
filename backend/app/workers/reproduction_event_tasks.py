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

# States during which the monitor should keep listening for FXS activity.
_LISTENING_STATES = {ReproductionState.WATCHING.value, ReproductionState.ACTIVITY_DETECTED.value}


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
    """Real-mode watcher: FXS events stream through the real platform.

    The platform owns the AsyncSSHDeviceAdapter on its dedicated bridge loop, so all
    asyncssh I/O (connect, AIM commands, raw AIM stream reads) share ONE event loop.
    A background reader pushes raw AIM chunks into a thread-safe queue and the
    synchronous orchestrator polls it — no cross-loop handoff, no deadlock.
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
    adapter = AsyncSSHDeviceAdapter(ip=device.ip, port=device.ssh_port, username=username, password=password)
    orch, _close = build_orchestrator(adapter=adapter, connect=True)
    try:
        # Start the bridge-loop AIM reader and wire its monitor into the orchestrator.
        orch.fxs_event_monitor = orch.platform.start_fxs_monitor()
        events_handled = 0
        started = time.monotonic()
        try:
            while time.monotonic() - started < max_seconds:
                row = _session_listening(db, session.id)
                if row is None or ReproductionState(row.state) not in _LISTENING_STATES:
                    break
                for ev in orch.fxs_event_monitor.poll():
                    handled = orch.record_fxs_event(db, session=row, event=ev, actor='reproduction-worker')
                    if handled is not None:
                        events_handled += 1
                    db.commit()
                if events_handled == 0:
                    await asyncio.sleep(0.2)
        finally:
            try:
                orch.platform.stop_fxs_monitor()
            except Exception:
                pass
        return {'status': 'DONE', 'session_id': session.id, 'events_handled': events_handled,
                'state': _session_listening(db, session.id).state if _session_listening(db, session.id) else 'GONE'}
    finally:
        _close()


@celery_app.task(name='reproduction.watch_fxs_events', bind=True, autoretry_for=(), max_retries=0)
def watch_fxs_events(self, session_id: str, max_seconds: int = 900):
    """Watch a reproduction session's DUT for FXS activity and feed it to the
    orchestrator as real activity anchors. Runs until the session leaves the
    watching/activity-detected states or the timeout elapses.
    """
    return asyncio.run(_watch(session_id, max_seconds=max_seconds))
