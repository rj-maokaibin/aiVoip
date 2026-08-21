import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import models as _models  # noqa: F401
from app.db.base import Base
from app.capture_v2.cleanup.coordinator import CaptureV2CleanupCoordinator, CleanupStep
from app.capture_v2.db_models import (
    CaptureEpoch, CaptureEvent, CaptureGap, CaptureSegment, CaptureSession, QualitySnapshot,
)
from app.capture_v2.enums import CaptureHealth, CaptureSessionState
from app.capture_v2.errors import CaptureV2Error
from app.capture_v2.runtime_coordinator import CaptureV2RuntimeCoordinator
from app.capture_v2.telemetry.snapshot import CaptureTelemetryCollector

T0 = datetime(2026, 8, 21, 0, 0, 0, tzinfo=timezone.utc)


def factory(*, state=CaptureSessionState.CLEANUP.value):
    engine = create_engine('sqlite+pysqlite:///:memory:', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    tables = [CaptureSession.__table__, CaptureEpoch.__table__, CaptureGap.__table__, CaptureSegment.__table__, CaptureEvent.__table__, QualitySnapshot.__table__]
    Base.metadata.create_all(engine, tables=tables)
    F = sessionmaker(bind=engine, expire_on_commit=False)
    with F() as db, db.begin():
        db.add(CaptureSession(
            id='S', reproduction_session_id='R', device_id='D', state=state,
            health_status=CaptureHealth.HEALTHY.value, capture_profile_id='p', capture_profile_version='1',
            platform_profile_id='mt7621', platform_profile_version='1', effective_profile={'resolved': {}},
            created_at=T0, path_ready_at=T0 + timedelta(seconds=2),
        ))
    return F


def test_cleanup_is_ordered_idempotent_and_release_is_last():
    F = factory()
    seen = []
    actions = {}
    for step in CleanupStep:
        async def action(step=step):
            seen.append(step.value)
            return True
        actions[step] = action
    c = CaptureV2CleanupCoordinator(F, actions=actions)
    asyncio.run(c.run(capture_session_id='S'))
    assert seen == [s.value for s in CleanupStep]
    # Retry performs no device mutation, including no second lease release.
    seen.clear()
    asyncio.run(c.run(capture_session_id='S'))
    assert seen == []
    with F() as db:
        row = db.get(CaptureSession, 'S')
        assert row.cleanup_status == 'VERIFIED'
        verified = [e for e in db.query(CaptureEvent).all() if e.event_type == 'CLEANUP_STEP_VERIFIED']
        assert len(verified) == len(CleanupStep)


def test_cleanup_failure_blocks_all_later_steps_and_lease_release():
    F = factory()
    seen = []
    actions = {}
    for step in CleanupStep:
        async def action(step=step):
            seen.append(step.value)
            if step == CleanupStep.DEBUG_OFF:
                return False
            return True
        actions[step] = action
    with pytest.raises(CaptureV2Error) as exc:
        asyncio.run(CaptureV2CleanupCoordinator(F, actions=actions).run(capture_session_id='S'))
    assert exc.value.code == 'CLEANUP_REVERSE_VERIFY_FAILED'
    assert seen == ['PCM_RX_OFF', 'PCM_TX_OFF', 'DEBUG_OFF']
    assert 'RELEASE_LEASE' not in seen
    with F() as db:
        assert db.get(CaptureSession, 'S').cleanup_status == 'FAILED'


def test_coverage_finalization_requires_durable_or_explicit_partial():
    # Import full runtime tables via the existing coordinator test factory so its
    # bridge dependencies are present; only the barrier behavior is asserted here.
    from tests.test_capture_v2_runtime_coordinator import factory as full_factory, ready_checks
    F, effective = full_factory()
    rt = CaptureV2RuntimeCoordinator(session_factory=F, capture_session_id='S', effective_profile=effective)
    rt.arm_to_watching(ready_checks())
    rt.begin_evidence_drain()
    with pytest.raises(CaptureV2Error) as exc:
        rt.begin_coverage_finalizing()
    assert exc.value.code == 'EVIDENCE_NOT_DURABLE'
    rt.begin_coverage_finalizing(explicit_partial=True, partial_reason='TRANSFER_TIMEOUT')
    assert rt.sessions.state('S') == CaptureSessionState.COVERAGE_FINALIZING


def test_telemetry_projects_p0_metrics_and_multiple_producer_alert():
    F = factory(state=CaptureSessionState.WATCHING.value)
    with F() as db, db.begin():
        for i in (1, 2):
            db.add(CaptureEpoch(
                id=f'E{i}', capture_session_id='S', device_id='D', epoch_index=i,
                epoch_token=f'CAP{i}', lease_epoch_started=i, state='RUNNING', started_at=T0,
            ))
        db.add(CaptureGap(
            id='G', capture_session_id='S', capture_epoch_id='E1', channel='PCAP',
            certainty='CONFIRMED', reason_code='PRODUCER_DIED', source='RECOVERY', detected_at=T0,
        ))
        db.add(CaptureSegment(
            id='SEG', capture_session_id='S', capture_epoch_id='E1', device_id='D', segment_seq=1,
            remote_path='/x', remote_inode=1, remote_size=1024, state='DISCOVERED',
            discovered_at=T0 + timedelta(seconds=10), last_error_code='SFTP_DISCONNECTED',
        ))
        db.add(QualitySnapshot(
            id='Q', idempotency_key='q', capture_session_id='S', capture_completeness='PARTIAL',
            diagnostic_confidence='MEDIUM', policy_version='1', reasons=[], created_at=T0 + timedelta(seconds=20),
        ))
    snap = CaptureTelemetryCollector(F).collect(
        device_id='D', now=T0 + timedelta(seconds=30), window_seconds=60, dut_spool_free_bytes=999,
    )
    assert snap.producer_count_per_dut == 2
    assert snap.capture_gap_total == 1
    assert snap.unacked_segment_count == 1
    assert snap.unacked_bytes == 1024
    assert snap.sftp_failure_rate == 1.0
    assert snap.capture_partial_rate == 1.0
    assert snap.ready_prepare_latency == 2.0
    assert snap.alerts == ('P0_MULTIPLE_PRODUCERS_PER_DUT',)
