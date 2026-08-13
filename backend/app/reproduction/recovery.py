from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.contracts.enums import LockStatus, ReproductionEvent, ReproductionState
from app.db.models import DeviceDiagnosticLock, ReproductionSession
from app.reproduction.orchestrator import ReproductionOrchestrator
from app.reproduction.state_machine import TERMINAL_STATES, next_state, transition_session


def _utcnow(): return datetime.now(timezone.utc)

def _aware(value):
    if value is not None and value.tzinfo is None: return value.replace(tzinfo=timezone.utc)
    return value


class RecoveryReconciler:
    def __init__(self, orchestrator: ReproductionOrchestrator | None = None):
        self.orchestrator=orchestrator or ReproductionOrchestrator()

    def reconcile_expired_leases(self, db: Session, *, actor: str='recovery-reconciler') -> list[str]:
        now=_utcnow(); recovered=[]
        locks=list(db.scalars(select(DeviceDiagnosticLock).where(DeviceDiagnosticLock.status==LockStatus.ACTIVE.value)))
        for lock in locks:
            if (_aware(lock.lease_expires_at) or now)>now: continue
            lock.status=LockStatus.EXPIRED.value
            session=db.get(ReproductionSession,lock.session_id)
            if not session or ReproductionState(session.state) in TERMINAL_STATES:
                continue
            try:
                next_state(ReproductionState(session.state),ReproductionEvent.LEASE_EXPIRED)
            except Exception:
                # Waiting states don't hold an active lease by contract; stale lock is enough to mark expired.
                continue
            transition_session(db,session,ReproductionEvent.LEASE_EXPIRED,actor=actor,reason='lease_expired_recovery')
            session.terminal_reason='LEASE_EXPIRED'
            self.orchestrator.cleanup(db,session=session,actor=actor)
            recovered.append(session.id)
        return recovered

    def retry_failed_cleanups(self, db: Session, *, actor: str='cleanup-watchdog') -> list[str]:
        rows=list(db.scalars(select(ReproductionSession).where(ReproductionSession.state.in_([
            ReproductionState.CLEANUP_FAILED.value,ReproductionState.CLEANUP_DEGRADED.value,ReproductionState.ORPHANED.value,
        ]))))
        result=[]
        for session in rows:
            self.orchestrator.retry_cleanup(db,session=session,actor=actor)
            result.append(session.id)
        return result
