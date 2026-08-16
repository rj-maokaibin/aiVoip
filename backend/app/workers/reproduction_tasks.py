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


@celery_app.task(name='fix_verification.schedule_reproduction', bind=True,
                 autoretry_for=(), max_retries=3)
def schedule_fix_verification_reproduction(self, verification_id: str):
    """Idempotently create and dispatch the reproduction bound to a fix check."""
    from app.contracts.enums import FixVerificationStatus
    from app.db.models import FixVerificationRun
    from app.services.audit import audit

    with SessionLocal() as db:
        verification = db.scalar(select(FixVerificationRun).where(
            FixVerificationRun.id == verification_id
        ).with_for_update())
        if verification is None:
            # The Feishu event transaction may still be committing when this
            # asynchronous task is first delivered.
            raise self.retry(exc=RuntimeError('FIX_VERIFICATION_NOT_COMMITTED'), countdown=1)
        if verification.verification_session_id:
            return {'status': 'ALREADY_SCHEDULED', 'verification_id': verification.id,
                    'session_id': verification.verification_session_id}
        baseline = db.get(ReproductionSession, verification.baseline_session_id)
        if baseline is None:
            return {'status': 'FAILED', 'reason': 'BASELINE_SESSION_NOT_FOUND',
                    'verification_id': verification.id}
        session = ReproductionOrchestrator().create_session(
            db, case_id=verification.case_id,
            profile_id=verification.reproduction_profile_id,
            device_id=baseline.device_id, actor='fix-verification-scheduler',
            retry_parent_session_id=baseline.id,
        )
        verification.verification_session_id = session.id
        verification.status = FixVerificationStatus.RUNNING.value
        audit(db, case_id=verification.case_id, actor='fix-verification-scheduler',
              event_type='FIX_VERIFICATION_REPRODUCTION_SCHEDULED',
              target_type='fix_verification', target_id=verification.id,
              detail={'session_id': session.id, 'baseline_session_id': baseline.id,
                      'profile_id': verification.reproduction_profile_id})
        db.commit()
        start_reproduction.apply_async(args=[session.id], queue='reproduction-control')
        try:
            from app.workers.device_provision_task import sync_case_card
            sync_case_card.apply_async(
                args=[verification.case_id, 'fix_verification_scheduled'], queue='diagnosis'
            )
        except Exception:
            pass
        return {'status': 'SCHEDULED', 'verification_id': verification.id,
                'session_id': session.id}


def _fix_environment(db, session, call) -> dict:
    from app.db.models import CaseDevice
    device = db.get(CaseDevice, session.device_id) if session else None
    info = dict((device.device_info or {}) if device else {})
    voice = dict((session.voice_runtime_context_json or {}) if session else {})
    metrics = dict(((call.quick_analysis_json or {}).get('metrics') or {}) if call else {})
    return {
        'device': {'serial': device.sn if device else None,
                   'model': info.get('model') or info.get('product_model')},
        'software': {'version': info.get('software_version') or info.get('version')},
        'voice': {'voice_vlan_id': voice.get('voice_vlan_id'),
                  'gateway_ip': voice.get('voice_gateway_ip'),
                  'fxs_port': metrics.get('fxs_port') or info.get('fxs_port')},
        'call': {'codec': metrics.get('codec'), 'called_number': metrics.get('called_number')},
    }


def ensure_fix_verification_evaluation(session_id: str) -> dict:
    """Best-effort deterministic evaluation for a scheduled fix reproduction."""
    from app.db.models import FixVerificationRun, ReproductionCall
    from app.experiments.fix_verification import FixVerificationService

    with SessionLocal() as db:
        verification = db.scalar(select(FixVerificationRun).where(
            FixVerificationRun.verification_session_id == session_id,
            FixVerificationRun.status == 'RUNNING',
        ).order_by(FixVerificationRun.updated_at.desc()).limit(1))
        if verification is None:
            return {'status': 'NO_FIX_VERIFICATION', 'session_id': session_id}
        current_session = db.get(ReproductionSession, session_id)
        baseline_session = db.get(ReproductionSession, verification.baseline_session_id)
        baseline_call = db.get(ReproductionCall, verification.baseline_call_id)
        current_call = db.scalar(select(ReproductionCall).where(
            ReproductionCall.session_id == session_id,
            ReproductionCall.status == 'ANALYZED',
        ).order_by(ReproductionCall.started_at.desc()).limit(1))
        if not current_session or not baseline_session or not baseline_call or not current_call:
            return {'status': 'WAITING_ANALYZED_CALL', 'verification_id': verification.id}
        qa = current_call.quick_analysis_json or {}
        findings = set(qa.get('findings') or [])
        blocking = ['HARD_CONTRADICTION'] if qa.get('hard_contradiction') else []
        try:
            result = FixVerificationService().evaluate(
                db, verification=verification,
                verification_session_id=current_session.id,
                verification_call_id=current_call.id,
                baseline_environment=_fix_environment(db, baseline_session, baseline_call),
                verification_environment=_fix_environment(db, current_session, current_call),
                business_checks={
                    'call_analyzed': True,
                    'target_absent': verification.target_finding not in findings,
                    'no_hard_contradiction': not bool(blocking),
                },
                new_blocking_findings=blocking,
                actor='fix-verification-worker',
            )
            db.commit()
        except Exception as exc:
            db.rollback()
            return {'status': 'EVALUATION_DEFERRED', 'verification_id': verification.id,
                    'reason': f'{type(exc).__name__}:{exc}'}
        try:
            from app.workers.device_provision_task import sync_case_card
            sync_case_card.apply_async(
                args=[verification.case_id, 'fix_verification_evaluated'], queue='diagnosis'
            )
        except Exception:
            pass
        return {'status': result.status, 'verification_id': result.id,
                'session_id': session_id, 'call_id': current_call.id}


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
        fix_verification = ensure_fix_verification_evaluation(session_id)
        # Idempotency: if this case already has a diagnosis run created after this
        # session started, its evidence was already consumed -- do not re-diagnose.
        anchor = session.started_at or session.created_at
        existing = db.scalar(select(DiagnosisRun).where(
            DiagnosisRun.case_id == session.case_id,
            DiagnosisRun.created_at >= anchor,
        ).order_by(DiagnosisRun.created_at.desc()).limit(1))
        if existing is not None:
            return {"status": "ALREADY_DIAGNOSED", "run_id": existing.id,
                    "session_id": session_id, "fix_verification": fix_verification}
        job, run = create_diagnosis_job(db, case_id=session.case_id)
        run_diagnosis.apply_async(args=[run.id], queue="diagnosis")
        return {"status": "TRIGGERED", "job_id": job.id, "run_id": run.id,
                "case_id": session.case_id, "session_id": session_id,
                "fix_verification": fix_verification}




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


@celery_app.task(name='reproduction.start', bind=True, queue='reproduction-control', autoretry_for=(DeviceConnectionError, DeviceCommandError),
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
            watch_fxs_events.apply_async(args=[row.id], queue='reproduction-watch')
        # The session may have already finished during start (e.g. ARM_FAILED ->
        # immediate cleanup). Trigger diagnosis when it reached a terminal state.
        diag = ensure_reproduction_diagnosis(session_id)
        return {'session_id':row.id,'state':row.state,'diagnosis':diag}


@celery_app.task(name='reproduction.cancel', queue='reproduction-control-high')
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


@celery_app.task(name='reproduction.reconcile', queue='reproduction-control-high')
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
