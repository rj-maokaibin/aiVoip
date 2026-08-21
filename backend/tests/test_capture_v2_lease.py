from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# Load the repository's existing tables so Capture V2 ForeignKey targets are present
# in Base.metadata. The test creates only V2 tables; SQLite FK enforcement stays off.
from app.db import models as _existing_models  # noqa: F401
from app.db.base import Base
from app.capture_v2.db_models import CaptureEvent, CaptureLease, CaptureSession
from app.capture_v2.enums import CaptureHealth, CaptureSessionState
from app.capture_v2.errors import CaptureV2Error
from app.capture_v2.lease.manager import CaptureLeaseManager


@pytest.fixture
def db_factory():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        engine,
        tables=[CaptureSession.__table__, CaptureLease.__table__, CaptureEvent.__table__],
    )
    Factory = sessionmaker(bind=engine, expire_on_commit=False)
    with Factory() as db, db.begin():
        for sid, rid in (("S1", "R1"), ("S2", "R2")):
            db.add(
                CaptureSession(
                    id=sid,
                    reproduction_session_id=rid,
                    device_id="D1",
                    state=CaptureSessionState.CREATED.value,
                    health_status=CaptureHealth.HEALTHY.value,
                    capture_profile_id="voip-standard",
                    capture_profile_version="2.1.1",
                    platform_profile_id="mt7621",
                    platform_profile_version="1",
                    effective_profile={},
                )
            )
    return Factory


def test_lease_acquire_is_idempotent_for_same_live_owner_and_fences_other_worker(db_factory):
    mgr = CaptureLeaseManager(db_factory, ttl_seconds=30)
    t0 = datetime(2026, 8, 20, 0, 0, tzinfo=timezone.utc)
    a = mgr.acquire(device_id="D1", capture_session_id="S1", owner_worker_id="W1", now=t0)
    assert a.lease_epoch == 1

    again = mgr.acquire(device_id="D1", capture_session_id="S1", owner_worker_id="W1", now=t0 + timedelta(seconds=1))
    assert again.lease_epoch == 1

    with pytest.raises(CaptureV2Error) as exc:
        mgr.acquire(device_id="D1", capture_session_id="S2", owner_worker_id="W2", now=t0 + timedelta(seconds=2))
    assert exc.value.code == "LEASE_BUSY"


def test_expired_takeover_increments_epoch_and_old_token_is_fenced(db_factory):
    mgr = CaptureLeaseManager(db_factory, ttl_seconds=30)
    t0 = datetime(2026, 8, 20, 0, 0, tzinfo=timezone.utc)
    old = mgr.acquire(device_id="D1", capture_session_id="S1", owner_worker_id="W1", now=t0)
    new = mgr.acquire(device_id="D1", capture_session_id="S2", owner_worker_id="W2", now=t0 + timedelta(seconds=31))
    assert new.lease_epoch == old.lease_epoch + 1

    with pytest.raises(CaptureV2Error) as exc:
        mgr.renew(old, now=t0 + timedelta(seconds=32))
    assert exc.value.code == "LEASE_FENCED"

    with pytest.raises(CaptureV2Error) as exc:
        mgr.release(old, now=t0 + timedelta(seconds=32))
    assert exc.value.code == "LEASE_FENCED"

    renewed = mgr.renew(new, now=t0 + timedelta(seconds=32))
    assert renewed.lease_epoch == new.lease_epoch
    assert renewed.expires_at > new.expires_at


def test_release_then_reacquire_still_increments_epoch(db_factory):
    mgr = CaptureLeaseManager(db_factory, ttl_seconds=30)
    t0 = datetime(2026, 8, 20, 0, 0, tzinfo=timezone.utc)
    first = mgr.acquire(device_id="D1", capture_session_id="S1", owner_worker_id="W1", now=t0)
    mgr.release(first, now=t0 + timedelta(seconds=3))
    second = mgr.acquire(device_id="D1", capture_session_id="S2", owner_worker_id="W2", now=t0 + timedelta(seconds=4))
    assert second.lease_epoch == 2

    with db_factory() as db:
        row = db.get(CaptureLease, "D1")
        assert row.owner_worker_id == "W2"
        assert row.lease_epoch == 2
        events = db.execute(select(CaptureEvent).order_by(CaptureEvent.recorded_at)).scalars().all()
        assert any(e.event_type == "CAPTURE_LEASE_RELEASED" for e in events)


def test_validate_passes_live_owner_without_extending_lease(db_factory):
    """Server-side pre-mutation fence check (validate) must accept the live token
    and must NOT extend the lease term (that is renew's job)."""
    mgr = CaptureLeaseManager(db_factory, ttl_seconds=30)
    t0 = datetime(2026, 8, 20, 0, 0, tzinfo=timezone.utc)
    token = mgr.acquire(device_id="D1", capture_session_id="S1", owner_worker_id="W1", now=t0)
    validated = mgr.validate(token, now=t0 + timedelta(seconds=5))
    assert validated.lease_epoch == token.lease_epoch
    assert validated.expires_at == token.expires_at  # validate must not extend

    with db_factory() as db:
        row = db.get(CaptureLease, "D1")
        # SQLite stores naive; compare after normalizing both to UTC-agnostic.
        row_expiry = row.expires_at.replace(tzinfo=timezone.utc)
        assert row_expiry == token.expires_at
        assert row.version == 1  # no version bump either


def test_validate_fences_expired_token(db_factory):
    mgr = CaptureLeaseManager(db_factory, ttl_seconds=30)
    t0 = datetime(2026, 8, 20, 0, 0, tzinfo=timezone.utc)
    old = mgr.acquire(device_id="D1", capture_session_id="S1", owner_worker_id="W1", now=t0)
    with pytest.raises(CaptureV2Error) as exc:
        mgr.validate(old, now=t0 + timedelta(seconds=31))
    assert exc.value.code == "LEASE_FENCED"


def test_validate_fences_after_takeover(db_factory):
    mgr = CaptureLeaseManager(db_factory, ttl_seconds=30)
    t0 = datetime(2026, 8, 20, 0, 0, tzinfo=timezone.utc)
    old = mgr.acquire(device_id="D1", capture_session_id="S1", owner_worker_id="W1", now=t0)
    new = mgr.acquire(device_id="D1", capture_session_id="S2", owner_worker_id="W2", now=t0 + timedelta(seconds=31))
    assert new.lease_epoch == old.lease_epoch + 1
    # The new owner validates fine; the old token is fenced.
    assert mgr.validate(new, now=t0 + timedelta(seconds=32)).lease_epoch == new.lease_epoch
    with pytest.raises(CaptureV2Error) as exc:
        mgr.validate(old, now=t0 + timedelta(seconds=32))
    assert exc.value.code == "LEASE_FENCED"


def test_first_insert_collision_retries_then_surfaces_winner_as_lease_busy(monkeypatch, db_factory):
    """SELECT FOR UPDATE cannot lock a missing row; the PK collision loser retries.

    The second observation must surface the winner as LEASE_BUSY, not leak the
    original IntegrityError or incorrectly create a second authority term.
    """
    from sqlalchemy.exc import IntegrityError

    mgr = CaptureLeaseManager(db_factory, ttl_seconds=30)
    calls = {"n": 0}

    def fake_once(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise IntegrityError("INSERT", {}, RuntimeError("duplicate key"))
        raise CaptureV2Error("LEASE_BUSY", details={"device_id": "D1"})

    monkeypatch.setattr(mgr, "_acquire_once", fake_once)
    with pytest.raises(CaptureV2Error) as exc:
        mgr.acquire(device_id="D1", capture_session_id="S1", owner_worker_id="W1")
    assert exc.value.code == "LEASE_BUSY"
    assert calls["n"] == 2
