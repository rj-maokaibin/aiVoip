from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.contracts.enums import LockStatus
from app.core.errors import AppError
from app.db.models import DeviceDiagnosticLock, ReproductionSession


def _utcnow():
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def acquire_device_lock(
    db: Session,
    *,
    session: ReproductionSession,
    owner_worker: str,
    lease_seconds: int,
) -> DeviceDiagnosticLock:
    now=_utcnow()
    existing=db.scalar(select(DeviceDiagnosticLock).where(DeviceDiagnosticLock.device_id==session.device_id))
    if existing:
        if existing.status == LockStatus.QUARANTINED.value:
            raise AppError(
                'DEVICE_DIAGNOSTIC_QUARANTINED',
                details={'device_id':session.device_id,'cleanup_session_id':existing.session_id},
            )
        expires=_aware(existing.lease_expires_at)
        active=existing.status==LockStatus.ACTIVE.value and expires is not None and expires>now
        if active and existing.session_id != session.id:
            raise AppError(
                'DEVICE_DIAGNOSTIC_LOCKED',
                details={'device_id':session.device_id,'owner_session_id':existing.session_id,'lease_expires_at':expires.isoformat()},
            )
        # Expired/released lock can be atomically reassigned. Reuse row to preserve one-row-per-device invariant.
        existing.session_id=session.id
        existing.owner_worker=owner_worker
        existing.status=LockStatus.ACTIVE.value
        existing.acquired_at=now
        existing.heartbeat_at=now
        existing.lease_expires_at=now+timedelta(seconds=lease_seconds)
        existing.released_at=None
        row=existing
    else:
        row=DeviceDiagnosticLock(
            device_id=session.device_id,
            session_id=session.id,
            owner_worker=owner_worker,
            status=LockStatus.ACTIVE.value,
            acquired_at=now,
            heartbeat_at=now,
            lease_expires_at=now+timedelta(seconds=lease_seconds),
        )
        db.add(row)
    # Take the two row locks in the same order as every other lock-bearing
    # transaction (session -> device_diagnostic_locks) to avoid an AB-BA deadlock
    # on the DeviceDiagnosticLock.session_id FK parent row (ShareLock on
    # reproduction_sessions). Write the session mirror first, flush, then the lock.
    session.owner_worker=owner_worker
    session.heartbeat_at=now
    session.lease_expires_at=row.lease_expires_at
    try:
        db.flush()
    except IntegrityError as exc:
        raise AppError('DEVICE_DIAGNOSTIC_LOCKED', details={'device_id':session.device_id}) from exc
    db.flush()
    return row


def heartbeat_device_lock(db: Session, *, session: ReproductionSession, lease_seconds: int) -> DeviceDiagnosticLock:
    row=db.scalar(select(DeviceDiagnosticLock).where(
        DeviceDiagnosticLock.device_id==session.device_id,
        DeviceDiagnosticLock.session_id==session.id,
    ))
    if not row or row.status != LockStatus.ACTIVE.value:
        raise AppError('REPRODUCTION_LEASE_EXPIRED', details={'session_id':session.id})
    now=_utcnow()
    if (_aware(row.lease_expires_at) or now) <= now:
        row.status=LockStatus.EXPIRED.value
        db.flush()
        raise AppError('REPRODUCTION_LEASE_EXPIRED', details={'session_id':session.id})
    # Mirror the lease on the session row FIRST, then update the lock row. This
    # forces every lock-bearing transaction to take the two row locks in the same
    # order (session -> device_diagnostic_locks) as the cancel path does
    # (transition_session writes the session, then release_device_lock writes the
    # lock). Previously heartbeat wrote the lock row then the session row, while
    # cancel wrote the session row then the lock row: the reversed order produced
    # an AB-BA deadlock on "device_diagnostic_locks" (DeviceDiagnosticLock.session_id
    # -> reproduction_sessions.id FK takes a ShareLock on the parent row) whenever
    # cancel raced the 15s heartbeat of a WATCHING session.
    session.heartbeat_at=now
    session.lease_expires_at=now+timedelta(seconds=lease_seconds)
    # Two explicit flushes guarantee the row-lock acquisition ORDER (session first,
    # then device_diagnostic_locks), matching the cancel path so no AB-BA deadlock.
    db.flush()
    row.heartbeat_at=now
    row.lease_expires_at=session.lease_expires_at
    db.flush()
    return row


def release_device_lock(db: Session, *, session: ReproductionSession, cleanup_verified: bool) -> None:
    if not cleanup_verified:
        raise AppError('CLEANUP_VERIFICATION_FAILED', details={'session_id':session.id,'reason':'LOCK_RELEASE_REQUIRES_CLEANUP_VERIFIED'})
    row=db.scalar(select(DeviceDiagnosticLock).where(
        DeviceDiagnosticLock.device_id==session.device_id,
        DeviceDiagnosticLock.session_id==session.id,
    ))
    if not row:
        return
    now=_utcnow()
    # Consistent row-lock order (session first, then lock) avoids the AB-BA
    # deadlock on the lock's session FK ShareLock.
    session.lease_expires_at=None
    db.flush()
    row.status=LockStatus.RELEASED.value
    row.released_at=now
    row.heartbeat_at=now
    # Keep the row for audit but make it immediately reclaimable.
    row.lease_expires_at=now
    db.flush()


def quarantine_device_lock(db: Session, *, session: ReproductionSession) -> None:
    """Block new ARM while allowing Cleanup/Recovery to keep using the DUT."""
    row=db.scalar(select(DeviceDiagnosticLock).where(
        DeviceDiagnosticLock.device_id==session.device_id,
        DeviceDiagnosticLock.session_id==session.id,
    ))
    if row is None:
        return
    now=_utcnow()
    # Consistent row-lock order (session first, then lock).
    session.lease_expires_at=None
    db.flush()
    row.status=LockStatus.QUARANTINED.value
    row.heartbeat_at=now
    # Quarantine is a safety interlock, not an expiring operation lease.
    row.lease_expires_at=now
    db.flush()
