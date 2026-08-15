from __future__ import annotations

import asyncio

from sqlalchemy import select

from app.collectors.asyncssh_adapter import DeviceCommandError, DeviceConnectionError
from app.core.config import settings
from app.db.models import ReproductionSession
from app.db.session import SessionLocal
from app.reproduction.orchestrator import ReproductionOrchestrator
from app.reproduction.recovery import RecoveryReconciler
from app.workers.celery_app import celery_app

# Reproduction sessions that ended with evidence are handed to the diagnosis
# worker automatically (closes the "reproduction finished but diagnosis must be
# clicked by hand" gap). These are the terminal states that can carry captured
# evidence worth diagnosing.
_DIAGNOSABLE_TERMINAL_STATES = ("COMPLETED", "PARTIAL_SUCCESS", "CANCELLED")


def ensure_reproduction_diagnosis(session_id: str) -> dict:
    """Idempotently trigger diagnosis after a reproduction session finished.

    Called when a reproduction session reaches a terminal, cleanup-verified state.
    Creates a DiagnosisRun + Job for the session's case and dispatches it to the
    diagnosis queue -- unless a diagnosis run created after this session started
    already exists (prevents duplicate auto-diagnosis while allowing a fresh run
    for a new session's evidence).

    Returns a status dict; never raises (caller must not fail the reproduction).
    """
    from app.contracts.enums import CleanupStatus
    from app.db.models import DiagnosisRun
    from app.services.diagnosis import create_diagnosis_job
    from app.workers.diagnosis_tasks import run_diagnosis

    with SessionLocal() as db:
        session = db.get(ReproductionSession, session_id)
        if session is None:
            return {"status": "NO_SESSION", "session_id": session_id}
        if session.state not in _DIAGNOSABLE_TERMINAL_STATES:
            return {"status": "NOT_TERMINAL", "state": session.state, "session_id": session_id}
        if session.cleanup_status != CleanupStatus.CLEANUP_VERIFIED.value:
            return {"status": "CLEANUP_NOT_VERIFIED", "cleanup_status": session.cleanup_status,
                    "session_id": session_id}
        # Idempotency: if this case already has a diagnosis run created after this
        # session started, its evidence was already consumed -- do not re-diagnose.
        anchor = session.started_at or session.created_at
        existing = db.scalar(select(DiagnosisRun).where(
            DiagnosisRun.case_id == session.case_id,
            DiagnosisRun.created_at >= anchor,
        ).order_by(DiagnosisRun.created_at.desc()).limit(1))
        if existing is not None:
            return {"status": "ALREADY_DIAGNOSED", "run_id": existing.id, "session_id": session_id}
        job, run = create_diagnosis_job(db, case_id=session.case_id)
        run_diagnosis.apply_async(args=[run.id], queue="diagnosis")
        return {"status": "TRIGGERED", "job_id": job.id, "run_id": run.id,
                "case_id": session.case_id, "session_id": session_id}




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


@celery_app.task(name='reproduction.start', bind=True, autoretry_for=(DeviceConnectionError, DeviceCommandError),
                 retry_backoff=True, retry_backoff_max=60, max_retries=3)
def start_reproduction(self, session_id: str):
    if settings.app_env.lower()=='production' and settings.reproduction_platform_mode=='mock':
        raise RuntimeError('REPRODUCTION_PLATFORM_NOT_CONFIGURED')
    with SessionLocal() as db:
        row=db.get(ReproductionSession,session_id)
        if not row: return {'status':'NOT_FOUND','session_id':session_id}
        orch, adapter, close = _build_orchestrator_for(row, connect=True)
        try:
            orch.start(db,session=row,owner_worker=f'celery:{self.request.id}',actor='reproduction-worker')
            db.commit()
        finally:
            close()
        # When the session reaches the watching state, hand FXS activity detection to
        # the dedicated watcher task on the reproduction worker queue.
        from app.contracts.enums import ReproductionState
        if ReproductionState(row.state) in {ReproductionState.WATCHING, ReproductionState.ACTIVITY_DETECTED}:
            from app.workers.reproduction_event_tasks import watch_fxs_events
            watch_fxs_events.apply_async(args=[row.id], queue='reproduction')
        # The session may have already finished during start (e.g. ARM_FAILED ->
        # immediate cleanup). Trigger diagnosis when it reached a terminal state.
        diag = ensure_reproduction_diagnosis(session_id)
        return {'session_id':row.id,'state':row.state,'diagnosis':diag}


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
        # A cancelled session may still hold captured evidence (e.g. a call was
        # analyzed before the user stopped) -- hand it to diagnosis.
        diag = ensure_reproduction_diagnosis(session_id)
        return {'session_id':row.id,'state':row.state,'cleanup_status':row.cleanup_status,
                'diagnosis':diag}


@celery_app.task(name='reproduction.reconcile', queue='reproduction')
def reconcile_reproduction():
    with SessionLocal() as db:
        r=RecoveryReconciler()
        recovered=r.reconcile_expired_leases(db)
        cleanup=r.retry_failed_cleanups(db)
        db.commit()
    # Watchdog recovery may have just finished a session (expired-lease recovery or
    # a cleanup retry that passed). Re-trigger diagnosis for any session that is
    # now terminal + cleanup-verified and has not been auto-diagnosed yet.
    triggered=0; skipped=0
    with SessionLocal() as db:
        rows=list(db.scalars(select(ReproductionSession).where(
            ReproductionSession.state.in_(_DIAGNOSABLE_TERMINAL_STATES))))
    for row in rows:
        res=ensure_reproduction_diagnosis(row.id)
        if res.get('status')=='TRIGGERED': triggered+=1
        else: skipped+=1
    return {'expired_lease_recovered':recovered,'cleanup_retried':cleanup,
            'diagnosis_triggered':triggered,'diagnosis_skipped':skipped}
