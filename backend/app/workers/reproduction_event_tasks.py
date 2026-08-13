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
from app.reproduction.orchestrator import ReproductionOrchestrator
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
        await adapter.connect()
        try:
            process = await adapter._ensure_aim_session(10)
            stream = process.stdout
            loop = asyncio.get_event_loop()

            def write_aim(cmd: str):
                process.stdin.write(cmd + '\n')

            monitor = FxsEventMonitor(read_aim_chunk=lambda: None, write_aim=write_aim)
            monitor.start()
            orch = ReproductionOrchestrator()
            events_handled = 0
            started = time.monotonic()
            try:
                while time.monotonic() - started < max_seconds:
                    # Check session state; stop if it left the listening states.
                    row = _session_listening(db, session_id)
                    if row is None or ReproductionState(row.state) not in _LISTENING_STATES:
                        break
                    try:
                        chunk = await asyncio.wait_for(stream.read(4096), 1.0)
                    except asyncio.TimeoutError:
                        chunk = ''
                    if chunk:
                        for ev in monitor.feed(chunk):
                            handled = orch.record_fxs_event(db, session=row, event=ev, actor='reproduction-worker')
                            if handled is not None:
                                events_handled += 1
                            db.commit()
            finally:
                monitor.stop()
            return {'status': 'DONE', 'session_id': session_id, 'events_handled': events_handled,
                    'state': _session_listening(db, session_id).state if _session_listening(db, session_id) else 'GONE'}
        finally:
            await adapter.disconnect()
    finally:
        db.close()


@celery_app.task(name='reproduction.watch_fxs_events', bind=True, autoretry_for=(), max_retries=0)
def watch_fxs_events(self, session_id: str, max_seconds: int = 900):
    """Watch a reproduction session's DUT for FXS activity and feed it to the
    orchestrator as real activity anchors. Runs until the session leaves the
    watching/activity-detected states or the timeout elapses.
    """
    return asyncio.run(_watch(session_id, max_seconds=max_seconds))
