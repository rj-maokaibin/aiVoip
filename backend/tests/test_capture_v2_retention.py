from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import models as _models  # noqa: F401
from app.db.base import Base
from app.capture_v2.db_models import CaptureEpoch, CaptureSegment, CaptureSession, CoverageWindow
from app.capture_v2.enums import CaptureHealth, CaptureSessionState
from app.capture_v2.segment.retention import SegmentRetentionService

T0 = datetime(2026, 8, 21, 0, 0, tzinfo=timezone.utc)


def factory():
    engine = create_engine('sqlite+pysqlite:///:memory:', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[CaptureSession.__table__, CaptureEpoch.__table__, CaptureSegment.__table__, CoverageWindow.__table__])
    F = sessionmaker(bind=engine, expire_on_commit=False)
    with F() as db, db.begin():
        db.add(CaptureSession(
            id='S', reproduction_session_id='R', device_id='D', state=CaptureSessionState.COVERAGE_FINALIZING.value,
            health_status=CaptureHealth.HEALTHY.value, capture_profile_id='p', capture_profile_version='1',
            platform_profile_id='mt7621', platform_profile_version='1', effective_profile={},
        ))
        db.add(CaptureEpoch(
            id='E1', capture_session_id='S', device_id='D', epoch_index=1, epoch_token='CAP1',
            lease_epoch_started=1, state='ENDED', started_at=T0, ended_at=T0 + timedelta(seconds=10),
        ))
        db.add(CaptureEpoch(
            id='E2', capture_session_id='S', device_id='D', epoch_index=2, epoch_token='CAP2',
            lease_epoch_started=2, state='ENDED', started_at=T0 + timedelta(seconds=20), ended_at=T0 + timedelta(seconds=30),
        ))
        db.add(CaptureSegment(
            id='A', capture_session_id='S', capture_epoch_id='E1', device_id='D', segment_seq=1,
            remote_path='/a', remote_inode=1, remote_size=24, state='ACKED', retention_state='ROLLING', discovered_at=T0,
        ))
        db.add(CaptureSegment(
            id='B', capture_session_id='S', capture_epoch_id='E2', device_id='D', segment_seq=1,
            remote_path='/b', remote_inode=2, remote_size=24, state='ACKED', retention_state='ROLLING', discovered_at=T0 + timedelta(seconds=20),
        ))
        db.add(CoverageWindow(
            id='W', idempotency_key='w', capture_session_id='S', window_type='ATTEMPT_EVIDENCE',
            required_start_ts=T0 + timedelta(seconds=2), required_end_ts=T0 + timedelta(seconds=8), status='COMPLETE',
        ))
    return F


def test_retention_pins_whole_overlapping_epoch_including_silent_segment_and_releases_only_rolling():
    F = factory()
    service = SegmentRetentionService(F)
    assert service.pin_for_coverage_window('W') == ('A',)
    released = service.release_rolling_before(capture_session_id='S', cutoff=T0 + timedelta(seconds=25))
    assert released == ('B',)
    with F() as db:
        assert db.get(CaptureSegment, 'A').retention_state == 'PINNED'
        assert db.get(CaptureSegment, 'B').retention_state == 'RELEASED'
    assert service.release_pinned(capture_session_id='S') == ('A',)
