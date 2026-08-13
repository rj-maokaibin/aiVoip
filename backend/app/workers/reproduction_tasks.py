from __future__ import annotations

import asyncio

from app.core.config import settings
from app.db.models import ReproductionSession
from app.db.session import SessionLocal
from app.reproduction.orchestrator import ReproductionOrchestrator
from app.reproduction.recovery import RecoveryReconciler
from app.workers.celery_app import celery_app


def _build_real_adapter(session: ReproductionSession):
    """Construct (but do not connect) a real DUT adapter for the session's device."""
    from app.integrations.credentials import get_credential_provider, LocalSecretCredentialProvider
    from app.collectors.asyncssh_adapter import AsyncSSHDeviceAdapter
    from app.db.models import CaseDevice

    device = None
    with SessionLocal() as db:
        device = db.get(CaseDevice, session.device_id)
    if device is None:
        raise RuntimeError('DEVICE_NOT_FOUND')

    async def _resolve():
        provider = get_credential_provider()
        password = await provider.get_password(sn=device.sn, ip=device.ip)
        username = device.username
        if isinstance(provider, LocalSecretCredentialProvider):
            try:
                username = provider.resolve_username(ip=device.ip, fallback=username)
            except Exception:
                pass
        return username, password

    username, password = asyncio.run(_resolve())
    return AsyncSSHDeviceAdapter(ip=device.ip, port=device.ssh_port, username=username, password=password)


def _build_orchestrator_for(session: ReproductionSession, *, connect: bool = False):
    """Build an orchestrator for the configured platform mode (mock or real)."""
    from app.reproduction.platform_factory import build_orchestrator, resolve_platform_mode
    if resolve_platform_mode() == 'mock':
        return ReproductionOrchestrator(), None, (lambda: None)
    adapter = _build_real_adapter(session)
    orch, close = build_orchestrator(adapter=adapter, connect=connect)
    return orch, adapter, close


@celery_app.task(name='reproduction.start')
def start_reproduction(session_id: str):
    if settings.app_env.lower()=='production' and settings.reproduction_platform_mode=='mock':
        raise RuntimeError('REPRODUCTION_PLATFORM_NOT_CONFIGURED')
    with SessionLocal() as db:
        row=db.get(ReproductionSession,session_id)
        if not row: return {'status':'NOT_FOUND','session_id':session_id}
        orch, adapter, close = _build_orchestrator_for(row, connect=True)
        try:
            orch.start(db,session=row,owner_worker=f'celery:{start_reproduction.request.id}',actor='reproduction-worker')
            db.commit()
        finally:
            close()
        # When the session reaches the watching state, hand FXS activity detection to
        # the dedicated watcher task on the reproduction worker queue.
        from app.contracts.enums import ReproductionState
        if ReproductionState(row.state) in {ReproductionState.WATCHING, ReproductionState.ACTIVITY_DETECTED}:
            from app.workers.reproduction_event_tasks import watch_fxs_events
            watch_fxs_events.apply_async(args=[row.id], queue='reproduction')
        return {'session_id':row.id,'state':row.state}


@celery_app.task(name='reproduction.cancel')
def cancel_reproduction(session_id: str):
    with SessionLocal() as db:
        row=db.get(ReproductionSession,session_id)
        if not row: return {'status':'NOT_FOUND','session_id':session_id}
        orch, adapter, close = _build_orchestrator_for(row, connect=True)
        try:
            orch.cancel(db,session=row,actor='reproduction-worker')
            db.commit()
        finally:
            close()
        return {'session_id':row.id,'state':row.state,'cleanup_status':row.cleanup_status}


@celery_app.task(name='reproduction.reconcile')
def reconcile_reproduction():
    with SessionLocal() as db:
        r=RecoveryReconciler()
        recovered=r.reconcile_expired_leases(db)
        cleanup=r.retry_failed_cleanups(db)
        db.commit()
        return {'expired_lease_recovered':recovered,'cleanup_retried':cleanup}
