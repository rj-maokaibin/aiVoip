from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.contracts.enums import LockStatus, ReproductionEvent, ReproductionState
from app.db.models import DeviceDiagnosticLock, ReproductionSession
from app.reproduction.orchestrator import ReproductionOrchestrator
from app.reproduction.state_machine import LOCK_HOLDING_STATES, TERMINAL_STATES, next_state, transition_session


def _utcnow(): return datetime.now(timezone.utc)
def _aware(value):
    if value is not None and value.tzinfo is None: return value.replace(tzinfo=timezone.utc)
    return value


class RecoveryReconciler:
    def __init__(self, orchestrator: ReproductionOrchestrator | None = None):
        self.orchestrator=orchestrator or ReproductionOrchestrator()

    def reconcile_expired_leases(
        self,
        db: Session,
        *,
        actor: str='recovery-reconciler',
        exclude_session_ids: set[str] | None = None,
    ) -> list[str]:
        """Recover legacy reproduction leases, optionally excluding V2-owned sessions.

        Capture V2 sessions require a real DUT adopt/finalizer cleanup path and must
        never be passed to the default Mock/legacy orchestrator by the beat worker.
        The optional exclusion preserves all existing V1 behavior by default.
        """
        excluded=set(exclude_session_ids or ())
        now=_utcnow(); recovered=[]; recovered_ids=set()
        locks=list(db.scalars(select(DeviceDiagnosticLock).where(DeviceDiagnosticLock.status==LockStatus.ACTIVE.value)))
        for lock in locks:
            if lock.session_id in excluded: continue
            if (_aware(lock.lease_expires_at) or now)>now: continue
            lock.status=LockStatus.EXPIRED.value
            session=db.get(ReproductionSession,lock.session_id)
            if not session or ReproductionState(session.state) in TERMINAL_STATES:
                continue
            try:
                next_state(ReproductionState(session.state),ReproductionEvent.LEASE_EXPIRED)
            except Exception:
                continue
            transition_session(db,session,ReproductionEvent.LEASE_EXPIRED,actor=actor,reason='lease_expired_recovery')
            session.terminal_reason='LEASE_EXPIRED'
            self.orchestrator.cleanup(db,session=session,actor=actor)
            recovered.append(session.id)
            recovered_ids.add(session.id)

        active_lock_session_ids=set(db.scalars(
            select(DeviceDiagnosticLock.session_id).where(
                DeviceDiagnosticLock.status==LockStatus.ACTIVE.value
            )
        ))
        lock_holding_values=[state.value for state in LOCK_HOLDING_STATES]
        orphan_candidates=list(db.scalars(select(ReproductionSession).where(
            ReproductionSession.state.in_(lock_holding_values),
            ReproductionSession.lease_expires_at.is_not(None),
            ReproductionSession.lease_expires_at <= now,
        )))
        for session in orphan_candidates:
            if session.id in excluded: continue
            if session.id in recovered_ids or session.id in active_lock_session_ids:
                continue
            try:
                next_state(ReproductionState(session.state),ReproductionEvent.LEASE_EXPIRED)
            except Exception:
                continue
            transition_session(db,session,ReproductionEvent.LEASE_EXPIRED,actor=actor,
                               reason='session_lease_expired_without_active_lock',
                               payload={'active_lock_missing':True})
            session.terminal_reason='LEASE_EXPIRED'
            self.orchestrator.cleanup(db,session=session,actor=actor)
            recovered.append(session.id)
            recovered_ids.add(session.id)
        return recovered

    def retry_failed_cleanups(
        self,
        db: Session,
        *,
        actor: str='cleanup-watchdog',
        exclude_session_ids: set[str] | None = None,
    ) -> list[str]:
        excluded=set(exclude_session_ids or ())
        rows=list(db.scalars(select(ReproductionSession).where(ReproductionSession.state.in_([
            ReproductionState.CLEANUP_FAILED.value,ReproductionState.CLEANUP_DEGRADED.value,ReproductionState.ORPHANED.value,
        ]))))
        result=[]
        for session in rows:
            if session.id in excluded: continue
            self.orchestrator.retry_cleanup(db,session=session,actor=actor)
            result.append(session.id)
        return result
