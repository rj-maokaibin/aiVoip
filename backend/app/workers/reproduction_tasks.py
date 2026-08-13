from __future__ import annotations

from app.core.config import settings
from app.db.models import ReproductionSession
from app.db.session import SessionLocal
from app.reproduction.orchestrator import ReproductionOrchestrator
from app.reproduction.recovery import RecoveryReconciler
from app.workers.celery_app import celery_app


@celery_app.task(name='reproduction.start')
def start_reproduction(session_id: str):
    if settings.app_env.lower()=='production' and settings.reproduction_platform_mode=='mock':
        raise RuntimeError('REPRODUCTION_PLATFORM_NOT_CONFIGURED')
    with SessionLocal() as db:
        row=db.get(ReproductionSession,session_id)
        if not row: return {'status':'NOT_FOUND','session_id':session_id}
        ReproductionOrchestrator().start(db,session=row,owner_worker=f'celery:{start_reproduction.request.id}',actor='reproduction-worker')
        db.commit()
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
        ReproductionOrchestrator().cancel(db,session=row,actor='reproduction-worker')
        db.commit()
        return {'session_id':row.id,'state':row.state,'cleanup_status':row.cleanup_status}


@celery_app.task(name='reproduction.reconcile')
def reconcile_reproduction():
    with SessionLocal() as db:
        r=RecoveryReconciler()
        recovered=r.reconcile_expired_leases(db)
        cleanup=r.retry_failed_cleanups(db)
        db.commit()
        return {'expired_lease_recovered':recovered,'cleanup_retried':cleanup}
