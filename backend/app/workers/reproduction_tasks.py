from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.collectors.asyncssh_adapter import DeviceCommandError, DeviceConnectionError
from app.capture_v2.db_models import CaptureLease, CaptureSession
from app.capture_v2.runtime import (
    assert_selected_v2_live_capture_allowed,
    assert_v1_live_capture_allowed,
    capture_v2_enabled,
)
from app.contracts.enums import CleanupStatus, LockStatus, ReproductionEvent, ReproductionState
from app.core.config import settings
from app.core.errors import AppError
from app.db.models import DeviceDiagnosticLock, ReproductionSession
from app.db.session import SessionLocal
from app.reproduction.fail_closed import (
    fail_closed_startup,
    session_has_active_lock,
    session_has_any_progress_event,
)
from app.reproduction.orchestrator import ReproductionOrchestrator
from app.reproduction.recovery import RecoveryReconciler
from app.reproduction.state_machine import LOCK_HOLDING_STATES, TERMINAL_STATES, next_state, transition_session
from app.workers.celery_app import celery_app

_DIAGNOSABLE_TERMINAL_STATES = ("COMPLETED", "PARTIAL_SUCCESS", "CANCELLED")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value):
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


@celery_app.task(name='fix_verification.schedule_reproduction', bind=True,
                 autoretry_for=(), max_retries=3)
def schedule_fix_verification_reproduction(self, verification_id: str):
    from app.contracts.enums import FixVerificationStatus
    from app.db.models import FixVerificationRun
    from app.services.audit import audit

    with SessionLocal() as db:
        verification = db.scalar(select(FixVerificationRun).where(
            FixVerificationRun.id == verification_id
        ).with_for_update())
        if verification is None:
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
        return {'status': result.status, 'verification_id': verification.id,
                'session_id': session_id, 'call_id': current_call.id}


def ensure_reproduction_diagnosis(session_id: str) -> dict:
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
    from app.integrations.credentials import get_credential_provider, LocalSecretCredentialProvider
    from app.collectors.asyncssh_adapter import AsyncSSHDeviceAdapter
    from app.db.models import CaseDevice

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


def _build_orchestrator_for(
    session: ReproductionSession,
    *,
    connect: bool = False,
    force_legacy_platform: bool = False,
):
    from app.reproduction.platform_factory import build_orchestrator, resolve_platform_mode
    if resolve_platform_mode() == 'mock':
        return ReproductionOrchestrator(), None, (lambda: None)
    adapter = _build_real_adapter(session)
    orch, close = build_orchestrator(
        adapter=adapter,
        connect=connect,
        force_legacy_platform=force_legacy_platform,
    )
    return orch, adapter, close


def _build_v2_cleanup_orchestrator(session: ReproductionSession):
    """Build a cleanup-only V2 platform without requiring V2 to remain selected.

    Rollback/recovery must always be able to stop a residual V2 producer, even after
    the application authority has already been switched back to V1.  This helper is
    only called by the dedicated cleanup task and the platform refuses to create a
    new CaptureSession when no existing V2 session exists.
    """
    from app.capture_v2.production_platform import CaptureV2ProductionPlatform
    adapter = _build_real_adapter(session)
    worker_id = CaptureV2ProductionPlatform._worker_id(session.id)
    platform = CaptureV2ProductionPlatform(
        adapter=adapter,
        reproduction_session_id=session.id,
        worker_id=worker_id,
    )
    orch = ReproductionOrchestrator(
        platform=platform,
        pcm_cleanup_guard=platform.pcm_cleanup_guard,
    )
    platform.connect()
    return orch, platform


def _session_has_v2_capture(db, session_id: str) -> bool:
    return db.scalar(select(CaptureSession.id).where(
        CaptureSession.reproduction_session_id == session_id
    )) is not None


@celery_app.task(name='reproduction.start', bind=True, queue='reproduction-control', autoretry_for=(DeviceConnectionError, DeviceCommandError),
                 retry_backoff=True, retry_backoff_max=60, max_retries=3)
def start_reproduction(self, session_id: str):
    if settings.app_env.lower()=='production' and settings.reproduction_platform_mode=='mock':
        raise RuntimeError('REPRODUCTION_PLATFORM_NOT_CONFIGURED')
    real_mode = str(settings.reproduction_platform_mode or 'mock').lower() != 'mock'
    if real_mode:
        if capture_v2_enabled():
            assert_selected_v2_live_capture_allowed()
        else:
            assert_v1_live_capture_allowed()
    with SessionLocal() as db:
        row=db.get(ReproductionSession,session_id)
        if not row:
            return {'status':'NOT_FOUND','session_id':session_id}
        # Under V2 the real Production platform participates in ARM itself so the
        # fenced producer + valid PCAP header are proven before WATCHING is exposed.
        # Platform close stops only the controller renewer; the producer remains
        # continuous for watcher adoption with no capture gap.
        orch, adapter, close = _build_orchestrator_for(row, connect=True)
        # Record the platform that actually runs this session.  create_session
        # snapshots the default Mock platform id even in real mode, which makes
        # M7/reporting mis-classify a real-DUT session as mock.  Reflect the real
        # platform (V1 aim-real or V2 capture-v2) on the committed row.
        try:
            platform = getattr(orch, "platform", None)
            if platform is not None and getattr(platform, "platform_id", None):
                row.platform_profile_id = str(platform.platform_id)
                row.platform_profile_version = str(getattr(platform, "version", "") or row.platform_profile_version)
        except Exception:
            pass
        try:
            orch.start(db,session=row,owner_worker=f'celery:{self.request.id}',actor='reproduction-worker')
            db.commit()
        except Exception as exc:
            # Fail-closed safety net: a deterministic startup failure must never
            # leave the session silently in CREATED.  Transient SSH errors keep
            # Celery autoretry semantics -- they are only fail-closed after the
            # final retry exhausts.
            db.rollback()
            final_failure = self.request.retries >= int(self.max_retries or 0) or not isinstance(
                exc, (DeviceConnectionError, DeviceCommandError)
            )
            if final_failure:
                with SessionLocal() as db2:
                    fresh = db2.get(ReproductionSession, session_id)
                    if fresh is not None:
                        ownership = session_has_active_lock(db2, fresh.id) or _session_has_v2_capture(db2, fresh.id)
                        outcome = fail_closed_startup(
                            db2, session=fresh, error=exc,
                            actor='reproduction-worker', ownership=ownership,
                        )
                        db2.commit()
                        if outcome == 'NEEDS_CLEANUP':
                            cleanup_v2_reproduction.apply_async(
                                args=[session_id, 'START_FAILED_CLEANUP'], queue='reproduction-watch'
                            )
            raise
        finally:
            close()
        if ReproductionState(row.state) in {ReproductionState.WATCHING, ReproductionState.ACTIVITY_DETECTED}:
            from app.workers.reproduction_event_tasks import watch_fxs_events
            watch_fxs_events.apply_async(args=[row.id], queue='reproduction-watch')
        diag = ensure_reproduction_diagnosis(session_id)
        return {'session_id':row.id,'state':row.state,'diagnosis':diag,
                'capture_engine':'V2' if capture_v2_enabled() else 'V1'}


@celery_app.task(
    name='reproduction.v2_cleanup',
    bind=True,
    queue='reproduction-watch',
    autoretry_for=(DeviceConnectionError, DeviceCommandError),
    retry_backoff=True,
    retry_backoff_max=60,
    max_retries=3,
)
def cleanup_v2_reproduction(self, session_id: str, reason: str = 'V2_CLEANUP_SERIALIZED'):
    """Adopt/finalize V2 and perform reverse cleanup on the single watch queue.

    The watch worker is concurrency=1 in the production compose definition. A
    high-priority cancel can therefore change the DB state immediately, while the
    physical finalizer runs only after the current watcher has exited. Two OS
    processes never operate as one logical V2 controller concurrently.
    """
    with SessionLocal() as db:
        row=db.get(ReproductionSession,session_id)
        if not row:
            return {'status':'NOT_FOUND','session_id':session_id}
        state=ReproductionState(row.state)
        if state in TERMINAL_STATES and row.cleanup_status == CleanupStatus.CLEANUP_VERIFIED.value:
            return {'status':'ALREADY_CLEAN','session_id':session_id,'state':state.value}
        orch, platform = _build_v2_cleanup_orchestrator(row)
        try:
            state=ReproductionState(row.state)
            if state in {
                ReproductionState.CLEANUP_FAILED,
                ReproductionState.CLEANUP_DEGRADED,
                ReproductionState.ORPHANED,
            }:
                orch.retry_cleanup(db,session=row,actor='capture-v2-cleanup-worker')
            elif state == ReproductionState.CLEANUP:
                orch.cleanup(
                    db,session=row,actor='capture-v2-cleanup-worker',
                    already_cleanup_state=True,
                )
            elif state in TERMINAL_STATES:
                # Terminal but unverified is an inconsistent state. Do not invent a
                # business transition; fail closed so the operator/reconciler sees it.
                raise RuntimeError(f'V2_TERMINAL_CLEANUP_UNVERIFIED:{state.value}')
            else:
                try:
                    next_state(state, ReproductionEvent.CLEANUP_STARTED)
                except Exception as exc:
                    raise RuntimeError(f'V2_CLEANUP_STATE_NOT_ALLOWED:{state.value}') from exc
                row.terminal_reason = row.terminal_reason or reason
                transition_session(
                    db,row,ReproductionEvent.CLEANUP_STARTED,
                    actor='capture-v2-cleanup-worker',reason=reason,
                )
                orch.cleanup(
                    db,session=row,actor='capture-v2-cleanup-worker',
                    already_cleanup_state=True,
                )
            db.commit()
        finally:
            platform.disconnect()
    diag=ensure_reproduction_diagnosis(session_id)
    with SessionLocal() as db:
        current=db.get(ReproductionSession,session_id)
        return {
            'status':'CLEANUP_FINISHED',
            'session_id':session_id,
            'state':current.state if current else 'GONE',
            'cleanup_status':current.cleanup_status if current else None,
            'diagnosis':diag,
        }


@celery_app.task(name='reproduction.cancel', queue='reproduction-control-high')
def cancel_reproduction(session_id: str):
    # Route any session which already owns a Capture V2 record through serialized
    # V2 cleanup even if the global authority has since rolled back to V1.
    with SessionLocal() as db:
        row=db.get(ReproductionSession,session_id)
        if not row:
            return {'status':'NOT_FOUND','session_id':session_id}
        use_v2 = capture_v2_enabled() or _session_has_v2_capture(db, session_id)
        if use_v2:
            state=ReproductionState(row.state)
            if state in TERMINAL_STATES and row.cleanup_status == CleanupStatus.CLEANUP_VERIFIED.value:
                return {'session_id':row.id,'state':row.state,'cleanup_status':row.cleanup_status,
                        'status':'ALREADY_TERMINAL'}
            if state not in {
                ReproductionState.CLEANUP,
                ReproductionState.CLEANUP_FAILED,
                ReproductionState.CLEANUP_DEGRADED,
                ReproductionState.ORPHANED,
            }:
                row.terminal_reason='CANCEL_REQUESTED'
                transition_session(
                    db,row,ReproductionEvent.CANCEL_REQUESTED,
                    actor='reproduction-worker',reason='user_stop_requested',
                )
            db.commit()
            state_value=row.state
            cleanup_status=row.cleanup_status
        else:
            orch, adapter, close = _build_orchestrator_for(row, connect=True)
            try:
                orch.cancel(db,session=row,actor='reproduction-worker')
                db.commit()
            finally:
                close()
            diag = ensure_reproduction_diagnosis(session_id)
            return {'session_id':row.id,'state':row.state,'cleanup_status':row.cleanup_status,
                    'diagnosis':diag}

    cleanup_v2_reproduction.apply_async(
        args=[session_id, 'USER_CANCELLED_V2'], queue='reproduction-watch'
    )
    return {
        'session_id':session_id,
        'state':state_value,
        'cleanup_status':cleanup_status,
        'status':'V2_CLEANUP_QUEUED',
    }


def _mark_v2_recovery_needed(db, v2_session_ids: set[str]) -> tuple[list[str], list[str]]:
    if not v2_session_ids:
        return [], []
    now=_utcnow()
    recovered:set[str]=set()

    # Capture lease expiry is the earliest V2 controller-loss signal. Expire the
    # mirrored business lock too so a stale watcher cannot keep heartbeating the
    # session after its fenced Capture authority was lost.
    expired_capture_leases=list(db.scalars(select(CaptureLease).where(
        CaptureLease.state == 'ACTIVE',
        CaptureLease.expires_at <= now,
    )))
    for lease in expired_capture_leases:
        capture=db.get(CaptureSession, lease.capture_session_id)
        if capture is None or capture.reproduction_session_id not in v2_session_ids:
            continue
        session=db.get(ReproductionSession,capture.reproduction_session_id)
        if session is None or ReproductionState(session.state) in TERMINAL_STATES:
            continue
        lock=db.scalar(select(DeviceDiagnosticLock).where(
            DeviceDiagnosticLock.session_id == session.id,
            DeviceDiagnosticLock.device_id == session.device_id,
        ))
        if lock is not None and lock.status == LockStatus.ACTIVE.value:
            lock.status=LockStatus.EXPIRED.value
        try:
            next_state(ReproductionState(session.state),ReproductionEvent.LEASE_EXPIRED)
        except Exception:
            pass
        else:
            transition_session(
                db,session,ReproductionEvent.LEASE_EXPIRED,
                actor='capture-v2-recovery-reconciler',
                reason='capture_v2_lease_expired',
            )
            session.terminal_reason='LEASE_EXPIRED'
        recovered.add(session.id)

    # Business diagnostic lock expiry/missing-lock mirror remains a second recovery
    # backstop, matching the legacy reconciler but without executing Mock cleanup.
    locks=list(db.scalars(select(DeviceDiagnosticLock).where(
        DeviceDiagnosticLock.status == LockStatus.ACTIVE.value,
        DeviceDiagnosticLock.session_id.in_(v2_session_ids),
    )))
    for lock in locks:
        if (_aware(lock.lease_expires_at) or now) > now:
            continue
        lock.status=LockStatus.EXPIRED.value
        session=db.get(ReproductionSession,lock.session_id)
        if session is None or ReproductionState(session.state) in TERMINAL_STATES:
            continue
        try:
            next_state(ReproductionState(session.state),ReproductionEvent.LEASE_EXPIRED)
        except Exception:
            continue
        transition_session(
            db,session,ReproductionEvent.LEASE_EXPIRED,
            actor='capture-v2-recovery-reconciler',reason='business_lease_expired_v2',
        )
        session.terminal_reason='LEASE_EXPIRED'
        recovered.add(session.id)

    active_lock_ids=set(db.scalars(select(DeviceDiagnosticLock.session_id).where(
        DeviceDiagnosticLock.status == LockStatus.ACTIVE.value,
        DeviceDiagnosticLock.session_id.in_(v2_session_ids),
    )))
    candidates=list(db.scalars(select(ReproductionSession).where(
        ReproductionSession.id.in_(v2_session_ids),
        ReproductionSession.state.in_([s.value for s in LOCK_HOLDING_STATES]),
        ReproductionSession.lease_expires_at.is_not(None),
        ReproductionSession.lease_expires_at <= now,
    )))
    for session in candidates:
        if session.id in recovered or session.id in active_lock_ids:
            continue
        try:
            next_state(ReproductionState(session.state),ReproductionEvent.LEASE_EXPIRED)
        except Exception:
            continue
        transition_session(
            db,session,ReproductionEvent.LEASE_EXPIRED,
            actor='capture-v2-recovery-reconciler',
            reason='session_lease_expired_without_active_lock_v2',
            payload={'active_lock_missing':True},
        )
        session.terminal_reason='LEASE_EXPIRED'
        recovered.add(session.id)

    retry_ids=set(db.scalars(select(ReproductionSession.id).where(
        ReproductionSession.id.in_(v2_session_ids),
        ReproductionSession.state.in_([
            ReproductionState.CLEANUP_FAILED.value,
            ReproductionState.CLEANUP_DEGRADED.value,
            ReproductionState.ORPHANED.value,
        ]),
    )))
    return sorted(recovered), sorted(retry_ids | recovered)


def _fail_closed_stale_created(db) -> list[str]:
    """Watchdog: fail-closed any CREATED session with no legal progress.

    A session left in CREATED beyond the stale threshold with no state events, no
    active lock and no Capture record has no legitimate in-flight Celery/ARM work;
    it is audibly fail-closed to ARM_FAILED.  Sessions that already own a lock or
    Capture record are left to the formal recovery/cleanup paths.
    """
    stale_seconds = float(getattr(settings, "reproduction_stale_created_seconds", 300.0) or 300.0)
    cutoff = _utcnow() - timedelta(seconds=stale_seconds)
    rows = list(db.scalars(select(ReproductionSession).where(
        ReproductionSession.state == ReproductionState.CREATED.value,
    )))
    closed: list[str] = []
    for session in rows:
        created = _aware(session.created_at)
        if created is None or created > cutoff:
            continue
        if session_has_any_progress_event(db, session.id):
            continue  # a START_ARMING event means legitimate ARM progress
        if session_has_active_lock(db, session.id) or _session_has_v2_capture(db, session.id):
            continue  # ownership exists -> formal recovery/cleanup paths
        outcome = fail_closed_startup(
            db, session=session,
            error=AppError("STALE_CREATED_NO_PROGRESS", details={"stale_created_seconds": stale_seconds}),
            actor="reconcile-watchdog", ownership=False,
        )
        if outcome == "FAIL_CLOSED":
            closed.append(session.id)
    return closed


@celery_app.task(name='reproduction.reconcile', queue='reproduction-control-high')
def reconcile_reproduction():
    with SessionLocal() as db:
        v2_session_ids=set(db.scalars(select(CaptureSession.reproduction_session_id)))
        v2_recovered, v2_cleanup_ids=_mark_v2_recovery_needed(db,v2_session_ids)
        r=RecoveryReconciler()
        recovered=r.reconcile_expired_leases(db,exclude_session_ids=v2_session_ids)
        cleanup=r.retry_failed_cleanups(db,exclude_session_ids=v2_session_ids)
        stale_created=_fail_closed_stale_created(db)
        db.commit()

    for session_id in v2_cleanup_ids:
        cleanup_v2_reproduction.apply_async(
            args=[session_id, 'V2_RECOVERY_RECONCILE'], queue='reproduction-watch'
        )

    triggered=0; skipped=0
    with SessionLocal() as db:
        rows=list(db.scalars(select(ReproductionSession).where(
            ReproductionSession.state.in_(_DIAGNOSABLE_TERMINAL_STATES))))
    for row in rows:
        res=ensure_reproduction_diagnosis(row.id)
        if res.get('status')=='TRIGGERED':
            triggered+=1
        else:
            skipped+=1
    return {
        'expired_lease_recovered':recovered,
        'cleanup_retried':cleanup,
        'v2_recovery_marked':v2_recovered,
        'v2_cleanup_queued':v2_cleanup_ids,
        'stale_created_fail_closed':stale_created,
        'diagnosis_triggered':triggered,
        'diagnosis_skipped':skipped,
    }
