from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.contracts.enums import LockStatus
from app.db.base import Base
from app.db.models import Case, CaseDevice, DeviceDiagnosticLock, ReproductionSession
from app.workers.reproduction_event_tasks import (
    _device_lock_reassigned,
    _latch_first_end_anchor,
    _session_listening,
    _should_restart_ring_after_end,
    watch_fxs_events,
)


def _engine():
    eng = create_engine(
        'sqlite+pysqlite:///:memory:',
        poolclass=StaticPool,
        connect_args={'check_same_thread': False},
    )
    Base.metadata.create_all(eng)
    return eng


def test_watch_fxs_events_task_is_registered():
    assert watch_fxs_events.name == 'reproduction.watch_fxs_events'


def test_first_onhook_end_anchor_wins_over_hook_bounce():
    assert _latch_first_end_anchor(None, 70_204) == 70_204
    assert _latch_first_end_anchor(70_204, 70_736) == 70_204


def test_no_call_end_restarts_ring_only_when_session_resumes_watching():
    assert _should_restart_ring_after_end('WATCHING', None) is True
    assert _should_restart_ring_after_end('ACTIVITY_DETECTED', None) is True
    assert _should_restart_ring_after_end('CAPTURING', 'call-1') is False
    assert _should_restart_ring_after_end('COMPLETED', None) is False


def test_watch_missing_session_returns_not_found():
    # Missing session should return quickly without attempting a device connection.
    result = watch_fxs_events.apply(args=['no-such-session'], throw=False)
    assert result.status == 'SUCCESS'
    assert result.result == {'status': 'SESSION_NOT_FOUND', 'session_id': 'no-such-session',
                             'diagnosis': {'status': 'NO_SESSION', 'session_id': 'no-such-session'}}


def _lock_fixture(db):
    case = Case(case_no='D1-1', summary='d1', status='ANALYZING')
    db.add(case); db.flush()
    dev = CaseDevice(case_id=case.id, ip='192.0.2.1', ssh_port=22, sn='D1', username='root')
    db.add(dev); db.flush()
    sa = ReproductionSession(case_id=case.id, device_id=dev.id, profile_key='P', profile_version='1',
                             profile_checksum='c', effective_profile_snapshot={}, state='WATCHING')
    db.add(sa); db.flush()
    sb = ReproductionSession(case_id=case.id, device_id=dev.id, profile_key='P', profile_version='1',
                             profile_checksum='c', effective_profile_snapshot={}, state='WATCHING')
    db.add(sb); db.flush()
    return dev, sa, sb


def test_device_lock_reassigned_true_when_other_session_active():
    eng = _engine()
    with Session(eng) as db:
        dev, sa, sb = _lock_fixture(db)
        db.add(DeviceDiagnosticLock(
            device_id=dev.id, session_id=sb.id, owner_worker='w',
            status=LockStatus.ACTIVE.value,
            lease_expires_at=datetime.now(timezone.utc) + timedelta(seconds=60),
        ))
        db.commit()
        assert _device_lock_reassigned(db, sa) is True
        assert _device_lock_reassigned(db, sb) is False  # owns the lock


def test_device_lock_reassigned_false_when_no_lock_or_expired():
    eng = _engine()
    with Session(eng) as db:
        dev, sa, sb = _lock_fixture(db)
        # No lock -> not reassigned.
        assert _device_lock_reassigned(db, sa) is False
        # Expired lock held by another session -> reclaimable, not reassigned.
        db.add(DeviceDiagnosticLock(
            device_id=dev.id, session_id=sb.id, owner_worker='w',
            status=LockStatus.ACTIVE.value,
            lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        ))
        db.commit()
        assert _device_lock_reassigned(db, sa) is False
        # RELEASED lock held by another session -> reclaimable.
        db.query(DeviceDiagnosticLock).update({DeviceDiagnosticLock.status: LockStatus.RELEASED.value})
        db.commit()
        assert _device_lock_reassigned(db, sa) is False


def test_session_listening_refreshes_state_changed_by_cancel_worker():
    eng = _engine()
    with Session(eng) as watcher_db:
        _dev, session, _other = _lock_fixture(watcher_db)
        watcher_db.commit()
        session_id = session.id

        # Seed the watcher's identity map with the pre-cancel state.
        assert watcher_db.get(ReproductionSession, session_id).state == 'WATCHING'

        with Session(eng) as cancel_db:
            cancelled = cancel_db.get(ReproductionSession, session_id)
            cancelled.state = 'CANCELLED'
            cancel_db.commit()

        # The watcher must observe the committed terminal state, not its cached
        # WATCHING object, so it exits before another segment/heartbeat operation.
        assert _session_listening(watcher_db, session_id).state == 'CANCELLED'
