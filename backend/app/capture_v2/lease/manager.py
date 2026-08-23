from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlalchemy.exc import IntegrityError

from app.capture_v2.db_models import CaptureLease
from app.capture_v2.enums import CaptureEventType, CaptureLeaseState
from app.capture_v2.errors import CaptureV2Error
from app.capture_v2.lease.repository import CaptureLeaseRepository
from app.capture_v2.repository.core import CaptureEventRepository


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class LeaseToken:
    device_id: str
    capture_session_id: str
    owner_worker_id: str
    lease_epoch: int
    expires_at: datetime


class CaptureLeaseManager:
    """DB authority for one active fenced controller per DUT.

    The lease row represents current authority; historical ownership changes are
    retained in CaptureEvent. An expired/released takeover always increments
    lease_epoch. A repeated acquire by the same live owner/session is idempotent.
    """

    def __init__(self, session_factory: Callable, *, ttl_seconds: float = 30.0, emit_renew_event: bool = False):
        if ttl_seconds < 10:
            raise ValueError("CAPTURE_LEASE_TTL_INVALID")
        self.session_factory = session_factory
        self.ttl_seconds = float(ttl_seconds)
        self.emit_renew_event = emit_renew_event

    def acquire(
        self,
        *,
        device_id: str,
        capture_session_id: str,
        owner_worker_id: str,
        now: datetime | None = None,
    ) -> LeaseToken:
        now = now or utcnow()
        # SELECT FOR UPDATE cannot lock a row which does not exist yet. Two workers
        # may race on the very first lease for a DUT; the PK collision loser retries
        # once and then observes the winner as LEASE_BUSY instead of leaking a raw
        # IntegrityError.
        try:
            return self._acquire_once(
                device_id=device_id,
                capture_session_id=capture_session_id,
                owner_worker_id=owner_worker_id,
                now=now,
            )
        except IntegrityError:
            try:
                return self._acquire_once(
                    device_id=device_id,
                    capture_session_id=capture_session_id,
                    owner_worker_id=owner_worker_id,
                    now=now,
                )
            except IntegrityError as exc:
                raise CaptureV2Error("LEASE_ACQUIRE_CONFLICT", details={"device_id": device_id}) from exc

    def _acquire_once(
        self,
        *,
        device_id: str,
        capture_session_id: str,
        owner_worker_id: str,
        now: datetime,
    ) -> LeaseToken:
        expires_at = now + timedelta(seconds=self.ttl_seconds)
        with self.session_factory() as db:
            with db.begin():
                repo = CaptureLeaseRepository(db)
                events = CaptureEventRepository(db)
                row = repo.get_for_update(device_id)
                previous = None
                if row is None:
                    row = CaptureLease(
                        device_id=device_id,
                        capture_session_id=capture_session_id,
                        owner_worker_id=owner_worker_id,
                        lease_epoch=1,
                        state=CaptureLeaseState.ACTIVE.value,
                        acquired_at=now,
                        renewed_at=now,
                        expires_at=expires_at,
                        version=1,
                    )
                    db.add(row)
                    db.flush()
                else:
                    previous = {
                        "capture_session_id": row.capture_session_id,
                        "owner_worker_id": row.owner_worker_id,
                        "lease_epoch": int(row.lease_epoch),
                        "state": row.state,
                    }
                    row_expiry = _aware(row.expires_at)
                    active_unexpired = (
                        row.state == CaptureLeaseState.ACTIVE.value
                        and row_expiry is not None
                        and row_expiry > now
                    )
                    same_owner = (
                        row.capture_session_id == capture_session_id
                        and row.owner_worker_id == owner_worker_id
                    )
                    if active_unexpired and not same_owner:
                        raise CaptureV2Error(
                            "LEASE_BUSY",
                            details={
                                "device_id": device_id,
                                "owner_worker_id": row.owner_worker_id,
                                "lease_epoch": int(row.lease_epoch),
                                "expires_at": row_expiry.isoformat(),
                            },
                        )
                    if active_unexpired and same_owner:
                        # Idempotent acquire also refreshes the live term so a caller
                        # retry near expiry does not immediately lose authority.
                        row.renewed_at = now
                        row.expires_at = expires_at
                        row.updated_at = now
                        row.version = int(row.version or 0) + 1
                        db.flush()
                        return LeaseToken(
                            device_id=device_id,
                            capture_session_id=capture_session_id,
                            owner_worker_id=owner_worker_id,
                            lease_epoch=int(row.lease_epoch),
                            expires_at=expires_at,
                        )
                    row.capture_session_id = capture_session_id
                    row.owner_worker_id = owner_worker_id
                    row.lease_epoch = int(row.lease_epoch) + 1
                    row.state = CaptureLeaseState.ACTIVE.value
                    row.acquired_at = now
                    row.renewed_at = now
                    row.expires_at = expires_at
                    row.updated_at = now
                    row.version = int(row.version or 0) + 1
                    db.flush()

                events.append(
                    capture_session_id=capture_session_id,
                    event_type=CaptureEventType.CAPTURE_LEASE_ACQUIRED,
                    entity_type="CAPTURE_LEASE",
                    entity_id=device_id,
                    source_ts=now,
                    payload={
                        "device_id": device_id,
                        "owner_worker_id": owner_worker_id,
                        "lease_epoch": int(row.lease_epoch),
                        "expires_at": expires_at.isoformat(),
                        "previous": previous,
                    },
                )
                token = LeaseToken(
                    device_id=device_id,
                    capture_session_id=capture_session_id,
                    owner_worker_id=owner_worker_id,
                    lease_epoch=int(row.lease_epoch),
                    expires_at=expires_at,
                )
            return token

    def renew(self, token: LeaseToken, *, now: datetime | None = None) -> LeaseToken:
        now = now or utcnow()
        expires_at = now + timedelta(seconds=self.ttl_seconds)
        with self.session_factory() as db:
            with db.begin():
                row = CaptureLeaseRepository(db).get_for_update(token.device_id)
                row_expiry = _aware(row.expires_at) if row is not None else None
                if (
                    row is None
                    or row.state != CaptureLeaseState.ACTIVE.value
                    or int(row.lease_epoch) != token.lease_epoch
                    or row.capture_session_id != token.capture_session_id
                    or row.owner_worker_id != token.owner_worker_id
                    or row_expiry is None
                    or row_expiry <= now
                ):
                    raise CaptureV2Error(
                        "LEASE_FENCED", details={"device_id": token.device_id, "lease_epoch": token.lease_epoch}
                    )
                row.renewed_at = now
                row.expires_at = expires_at
                row.updated_at = now
                row.version = int(row.version or 0) + 1
                if self.emit_renew_event:
                    CaptureEventRepository(db).append(
                        capture_session_id=token.capture_session_id,
                        event_type=CaptureEventType.CAPTURE_LEASE_RENEWED,
                        entity_type="CAPTURE_LEASE",
                        entity_id=token.device_id,
                        source_ts=now,
                        payload={"lease_epoch": token.lease_epoch, "expires_at": expires_at.isoformat()},
                    )
            return LeaseToken(
                device_id=token.device_id,
                capture_session_id=token.capture_session_id,
                owner_worker_id=token.owner_worker_id,
                lease_epoch=token.lease_epoch,
                expires_at=expires_at,
            )

    def validate(self, token: LeaseToken, *, now: datetime | None = None) -> LeaseToken:
        """Server-side authority check without extending the lease term.

        Unlike renew(), this is a pure read/fence check used immediately before a
        DUT mutation (e.g. producer stop or final seal) to reject a token that is
        no longer the live, unexpired owner. It never bumps expires_at or version;
        only renew() extends authority. Raises LEASE_FENCED when the DB row no
        longer matches the token (missing row, non-ACTIVE state, epoch/session/
        worker mismatch, or an expired term).
        """
        now = now or utcnow()
        with self.session_factory() as db:
            with db.begin():
                row = CaptureLeaseRepository(db).get_for_update(token.device_id)
                row_expiry = _aware(row.expires_at) if row is not None else None
                if (
                    row is None
                    or row.state != CaptureLeaseState.ACTIVE.value
                    or int(row.lease_epoch) != token.lease_epoch
                    or row.capture_session_id != token.capture_session_id
                    or row.owner_worker_id != token.owner_worker_id
                    or row_expiry is None
                    or row_expiry <= now
                ):
                    raise CaptureV2Error(
                        "LEASE_FENCED", details={"device_id": token.device_id, "lease_epoch": token.lease_epoch}
                    )
            return token

    def release(self, token: LeaseToken, *, now: datetime | None = None) -> None:
        now = now or utcnow()
        with self.session_factory() as db:
            with db.begin():
                row = CaptureLeaseRepository(db).get_for_update(token.device_id)
                if (
                    row is None
                    or row.state != CaptureLeaseState.ACTIVE.value
                    or int(row.lease_epoch) != token.lease_epoch
                    or row.capture_session_id != token.capture_session_id
                    or row.owner_worker_id != token.owner_worker_id
                ):
                    raise CaptureV2Error(
                        "LEASE_FENCED", details={"device_id": token.device_id, "lease_epoch": token.lease_epoch}
                    )
                row.state = CaptureLeaseState.RELEASED.value
                row.expires_at = now
                row.updated_at = now
                row.version = int(row.version or 0) + 1
                CaptureEventRepository(db).append(
                    capture_session_id=token.capture_session_id,
                    event_type=CaptureEventType.CAPTURE_LEASE_RELEASED,
                    entity_type="CAPTURE_LEASE",
                    entity_id=token.device_id,
                    source_ts=now,
                    payload={"lease_epoch": token.lease_epoch},
                )
