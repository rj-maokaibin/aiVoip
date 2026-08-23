from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import models as _existing_models  # noqa: F401
from app.db.base import Base
from app.capture_v2.coverage.calculator import EvidenceInterval
from app.capture_v2.coverage.ledger import CoverageLedgerService
from app.capture_v2.db_models import (
    CaptureEpoch, CaptureEvent, CaptureGap, CaptureSegment, CaptureSession,
    CoverageInterval, CoverageTrack, CoverageWindow,
    EvidenceAsset, QualitySnapshot, SignalAvailability,
)
from app.capture_v2.enums import (
    CaptureHealth, CaptureSessionState, CoverageIntervalType,
)
from app.capture_v2.finalizer import CaptureV2CaptureFinalizer
from app.capture_v2.quality.signals import SignalEvidence
from app.capture_v2.f_bridge import CaptureV2FQualityReporter
from app.capture_v2.report.evidence_first import EvidenceAssetRepository
from app.capture_v2.producer.manager import ProducerExitStats
from app.capture_v2.transfer.pump import PumpResult

T0 = datetime(2026, 8, 20, tzinfo=timezone.utc)


def factory(extra_tables=()):
    engine = create_engine(
        'sqlite+pysqlite:///:memory:',
        connect_args={'check_same_thread': False}, poolclass=StaticPool,
    )
    tables = [
        CaptureSession.__table__, CaptureEpoch.__table__, CaptureEvent.__table__, CaptureGap.__table__,
        CaptureSegment.__table__, CoverageWindow.__table__, CoverageTrack.__table__,
        CoverageInterval.__table__, QualitySnapshot.__table__, SignalAvailability.__table__,
        EvidenceAsset.__table__,
    ]
    Base.metadata.create_all(engine, tables=tables + list(extra_tables))
    F = sessionmaker(bind=engine, expire_on_commit=False)
    with F() as db, db.begin():
        db.add(CaptureSession(
            id='S', reproduction_session_id='R', device_id='D',
            state=CaptureSessionState.PREPARING.value,
            health_status=CaptureHealth.HEALTHY.value,
            capture_profile_id='p', capture_profile_version='1',
            platform_profile_id='mt7621', platform_profile_version='1',
            effective_profile={},
        ))
    return F


def test_coverage_recalculation_reuses_window_and_replaces_track_intervals():
    F = factory()
    ledger = CoverageLedgerService(F)
    wid1 = ledger.create_window(
        capture_session_id='S', capture_attempt_id=None, call_ref='C1',
        window_type='ATTEMPT_EVIDENCE', required_start_ts=T0,
        required_end_ts=T0 + timedelta(seconds=10), idempotency_key='W1',
    )
    wid2 = ledger.create_window(
        capture_session_id='S', capture_attempt_id=None, call_ref='C1',
        window_type='ATTEMPT_EVIDENCE', required_start_ts=T0,
        required_end_ts=T0 + timedelta(seconds=10), idempotency_key='W1',
    )
    assert wid1 == wid2

    ledger.calculate_track(
        coverage_window_id=wid1, channel='PCAP', requirement='REQUIRED',
        evidence=[EvidenceInterval(T0, T0 + timedelta(seconds=10), CoverageIntervalType.COVERED, 'EPOCH', 'E1')],
    )
    ledger.calculate_track(
        coverage_window_id=wid1, channel='PCAP', requirement='REQUIRED',
        evidence=[
            EvidenceInterval(T0, T0 + timedelta(seconds=10), CoverageIntervalType.COVERED, 'EPOCH', 'E1'),
            EvidenceInterval(T0 + timedelta(seconds=4), T0 + timedelta(seconds=5), CoverageIntervalType.GAP, 'GAP', 'G1'),
        ],
    )
    with F() as db:
        tracks = list(db.scalars(select(CoverageTrack)))
        intervals = list(db.scalars(select(CoverageInterval)))
        assert len(tracks) == 1
        assert tracks[0].status == 'PARTIAL'
        assert sum(1 for i in intervals if i.interval_type == 'GAP') == 1
        assert len(intervals) == 3


def test_quality_retry_with_same_semantics_returns_same_snapshot():
    F = factory()
    reporter = CaptureV2FQualityReporter(F)
    args = dict(
        capture_session_id='S', capture_attempt_id=None, call_ref='C1',
        capture_completeness='COMPLETE',
        signals=[SignalEvidence(channel='RTP', expected=True, captured=True, usable=True)],
        required_channels_for_diagnosis=('RTP',), independent_support_count=2,
    )
    q1, _ = reporter.evaluate_and_persist(**args)
    q2, _ = reporter.evaluate_and_persist(**args)
    assert q1 == q2
    with F() as db:
        assert len(list(db.scalars(select(QualitySnapshot)))) == 1
        assert len(list(db.scalars(select(SignalAvailability)))) == 1


def test_evidence_asset_idempotency_does_not_duplicate():
    F = factory()
    repo = EvidenceAssetRepository(F)
    kwargs = dict(
        capture_session_id='S', asset_type='ABNORMAL_AUDIO', title='abnormal.wav',
        storage_key='evidence/a.wav', source_refs=['SEG1'], idempotency_key='asset:1',
    )
    a1 = repo.create(**kwargs)
    a2 = repo.create(**kwargs)
    assert a1 == a2
    with F() as db:
        assert len(list(db.scalars(select(EvidenceAsset)))) == 1


class _ProducerManager:
    def __init__(self):
        self.stops = 0

    async def stop_identity(self, token, producer):
        self.stops += 1

    async def read_exit_stats(self, capture_epoch_token):
        return ProducerExitStats(10, 10, 0)


class _Pump:
    async def run_final_once(self, **kwargs):
        return PumpResult()


class _Lease:
    def validate(self, token):
        return token


@pytest.mark.asyncio
async def test_finalizer_retry_is_event_idempotent_and_durable_boundary_is_one_way():
    F = factory()
    with F() as db, db.begin():
        db.add(CaptureEpoch(
            id='E', capture_session_id='S', device_id='D', epoch_index=1,
            epoch_token='CAP_E', lease_epoch_started=1, state='RUNNING',
            started_at=T0, producer_pid=10, producer_starttime=20,
        ))
        db.add(CaptureSegment(
            id='SEG', capture_session_id='S', capture_epoch_id='E', device_id='D',
            segment_seq=1, remote_path='/tmp/seg.pcap', remote_inode=1,
            remote_size=24, state='ACKED', storage_key='k', server_size=24,
            sha256='0' * 64, packet_count=10,
        ))
    pm = _ProducerManager()
    finalizer = CaptureV2CaptureFinalizer(
        session_factory=F, producer_manager=pm, pump=_Pump(), lease_manager=_Lease(),
    )
    producer = SimpleNamespace(pid=10, process_starttime=20)
    token = SimpleNamespace(lease_epoch=1)
    r1 = await finalizer.finalize(
        capture_session_id='S', capture_epoch_id='E', capture_epoch_token='CAP_E',
        producer=producer, token=token,
    )
    r2 = await finalizer.finalize(
        capture_session_id='S', capture_epoch_id='E', capture_epoch_token='CAP_E',
        producer=producer, token=token,
    )
    assert r1.durable is True and r2.durable is True
    with F() as db:
        events = list(db.scalars(select(CaptureEvent)))
        assert sum(e.event_type == 'CAPTURE_EPOCH_ENDED' for e in events) == 1
        assert sum(e.event_type == 'EVIDENCE_DURABLE' for e in events) == 1
        session = db.get(CaptureSession, 'S')
        assert session.evidence_durable_at is not None


@pytest.mark.asyncio
async def test_finalizer_cannot_claim_durable_when_packets_were_captured_but_no_segment_exists():
    F = factory()
    with F() as db, db.begin():
        db.add(CaptureEpoch(
            id='E', capture_session_id='S', device_id='D', epoch_index=1,
            epoch_token='CAP_E', lease_epoch_started=1, state='RUNNING',
            started_at=T0, producer_pid=10, producer_starttime=20,
        ))
    finalizer = CaptureV2CaptureFinalizer(
        session_factory=F, producer_manager=_ProducerManager(), pump=_Pump(), lease_manager=_Lease(),
    )
    result = await finalizer.finalize(
        capture_session_id='S', capture_epoch_id='E', capture_epoch_token='CAP_E',
        producer=SimpleNamespace(pid=10, process_starttime=20), token=SimpleNamespace(lease_epoch=1),
    )
    assert result.durable is False

class _LateStatsProducerManager(_ProducerManager):
    def __init__(self):
        super().__init__()
        self.reads = 0

    async def read_exit_stats(self, capture_epoch_token):
        self.reads += 1
        if self.reads == 1:
            return ProducerExitStats(None, None, None)
        return ProducerExitStats(10, 10, 0)


@pytest.mark.asyncio
async def test_finalizer_retry_fills_exit_stats_that_were_unknown_on_first_finalize():
    F = factory()
    with F() as db, db.begin():
        db.add(CaptureEpoch(
            id='E', capture_session_id='S', device_id='D', epoch_index=1,
            epoch_token='CAP_E', lease_epoch_started=1, state='RUNNING',
            started_at=T0, producer_pid=10, producer_starttime=20,
        ))
        db.add(CaptureSegment(
            id='SEG', capture_session_id='S', capture_epoch_id='E', device_id='D',
            segment_seq=1, remote_path='/tmp/seg.pcap', remote_inode=1,
            remote_size=24, state='ACKED', storage_key='k', server_size=24,
            sha256='0' * 64, packet_count=10,
        ))
    pm = _LateStatsProducerManager()
    finalizer = CaptureV2CaptureFinalizer(
        session_factory=F, producer_manager=pm, pump=_Pump(), lease_manager=_Lease(),
    )
    kwargs = dict(
        capture_session_id='S', capture_epoch_id='E', capture_epoch_token='CAP_E',
        producer=SimpleNamespace(pid=10, process_starttime=20), token=SimpleNamespace(lease_epoch=1),
    )
    await finalizer.finalize(**kwargs)
    await finalizer.finalize(**kwargs)
    with F() as db:
        epoch = db.get(CaptureEpoch, 'E')
        assert epoch.packets_captured == 10
        assert epoch.packets_received == 10
        assert epoch.packets_dropped_kernel == 0
        events = list(db.scalars(select(CaptureEvent)))
        assert sum(e.event_type == 'CAPTURE_EPOCH_ENDED' for e in events) == 1


@pytest.mark.asyncio
async def test_prior_epoch_segment_cannot_mask_missing_segment_in_current_epoch():
    F = factory()
    with F() as db, db.begin():
        db.add(CaptureEpoch(
            id='OLD', capture_session_id='S', device_id='D', epoch_index=1,
            epoch_token='CAP_OLD', lease_epoch_started=1, state='ENDED',
            started_at=T0 - timedelta(seconds=20), ended_at=T0 - timedelta(seconds=10),
            packets_captured=1, packets_received=1, packets_dropped_kernel=0,
        ))
        db.add(CaptureSegment(
            id='OLDSEG', capture_session_id='S', capture_epoch_id='OLD', device_id='D',
            segment_seq=1, remote_path='/tmp/old.pcap', remote_inode=1,
            remote_size=24, state='ACKED', storage_key='old', server_size=24,
            sha256='0' * 64,
        ))
        db.add(CaptureEpoch(
            id='E', capture_session_id='S', device_id='D', epoch_index=2,
            epoch_token='CAP_E', lease_epoch_started=2, state='RUNNING',
            started_at=T0, producer_pid=10, producer_starttime=20,
        ))
    finalizer = CaptureV2CaptureFinalizer(
        session_factory=F, producer_manager=_ProducerManager(), pump=_Pump(), lease_manager=_Lease(),
    )
    result = await finalizer.finalize(
        capture_session_id='S', capture_epoch_id='E', capture_epoch_token='CAP_E',
        producer=SimpleNamespace(pid=10, process_starttime=20), token=SimpleNamespace(lease_epoch=2),
    )
    assert result.durable is False


def test_quality_production_path_is_bound_to_finalized_coverage_not_caller_claim():
    F = factory()
    with F() as db, db.begin():
        db.add(CoverageWindow(
            id='W', idempotency_key='W', capture_session_id='S', capture_attempt_id=None,
            call_ref='C1', window_type='ATTEMPT_EVIDENCE', required_start_ts=T0,
            required_end_ts=T0 + timedelta(seconds=10), status='PARTIAL',
            finalized_at=T0 + timedelta(seconds=11), details={},
        ))
    reporter = CaptureV2FQualityReporter(F)
    qid, quality = reporter.evaluate_from_coverage(
        coverage_window_id='W', capture_session_id='S', capture_attempt_id=None,
        call_ref='C1',
        signals=[SignalEvidence(channel='RTP', expected=True, captured=True, usable=True)],
        required_channels_for_diagnosis=('RTP',), independent_support_count=3,
    )
    assert quality['capture_completeness'] == 'PARTIAL'
    assert quality['diagnostic_confidence'] == 'MEDIUM'
    with F() as db:
        row = db.get(QualitySnapshot, qid)
        assert row.coverage_window_id == 'W'
        assert row.capture_completeness == 'PARTIAL'


def test_report_from_snapshot_uses_persisted_quality_not_free_form_quality_dict():
    F = factory()
    with F() as db, db.begin():
        db.add(CoverageWindow(
            id='W', idempotency_key='W', capture_session_id='S', capture_attempt_id=None,
            call_ref='C1', window_type='ATTEMPT_EVIDENCE', required_start_ts=T0,
            required_end_ts=T0 + timedelta(seconds=10), status='FAILED',
            finalized_at=T0 + timedelta(seconds=11), details={},
        ))
    reporter = CaptureV2FQualityReporter(F)
    qid, _ = reporter.evaluate_from_coverage(
        coverage_window_id='W', capture_session_id='S', capture_attempt_id=None,
        call_ref='C1', signals=[], required_channels_for_diagnosis=(),
        independent_support_count=0,
    )
    manifest = reporter.build_report_from_snapshot(
        capture_session_id='S', quality_snapshot_id=qid, findings=[]
    )
    assert manifest['quality']['capture_completeness'] == 'FAILED'
    assert manifest['quality']['quality_snapshot_id'] == qid


@pytest.mark.asyncio
async def test_finalizer_packet_accounting_mismatch_creates_possible_gap_and_blocks_durable():
    F = factory()
    with F() as db, db.begin():
        db.add(CaptureEpoch(
            id='EACC', capture_session_id='S', device_id='D', epoch_index=1,
            epoch_token='CAP_ACC', lease_epoch_started=1, state='RUNNING',
            started_at=T0, producer_pid=10, producer_starttime=20,
        ))
        db.add(CaptureSegment(
            id='SEGACC', capture_session_id='S', capture_epoch_id='EACC', device_id='D',
            segment_seq=1, remote_path='/tmp/seg.pcap', remote_inode=1,
            remote_size=24, state='ACKED', storage_key='k', server_size=24,
            sha256='0' * 64, packet_count=9,
        ))
    finalizer = CaptureV2CaptureFinalizer(
        session_factory=F, producer_manager=_ProducerManager(), pump=_Pump(), lease_manager=_Lease(),
    )
    result = await finalizer.finalize(
        capture_session_id='S', capture_epoch_id='EACC', capture_epoch_token='CAP_ACC',
        producer=SimpleNamespace(pid=10, process_starttime=20), token=SimpleNamespace(lease_epoch=1),
    )
    assert result.durable is False
    with F() as db:
        gaps = list(db.scalars(select(CaptureGap).where(
            CaptureGap.capture_epoch_id == 'EACC',
            CaptureGap.reason_code == 'PCAP_PACKET_ACCOUNTING_MISMATCH',
        )))
        assert len(gaps) == 1
        assert gaps[0].certainty == 'POSSIBLE'
        assert gaps[0].gap_start_ts is None
        assert gaps[0].details['tcpdump_packets_captured'] == 10
        assert gaps[0].details['segment_packet_count_sum'] == 9
