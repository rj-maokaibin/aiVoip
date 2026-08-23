from datetime import datetime, timedelta, timezone

from app.capture_v2.coverage.calculator import CoverageCalculator, EvidenceInterval
from app.capture_v2.enums import CoverageIntervalType, CoverageStatus


T0 = datetime(2026, 8, 20, tzinfo=timezone.utc)


def test_traffic_silence_inside_running_epoch_is_not_gap():
    result = CoverageCalculator.calculate(
        required_start=T0, required_end=T0 + timedelta(seconds=10),
        evidence=[EvidenceInterval(T0, T0 + timedelta(seconds=10), CoverageIntervalType.COVERED,
                                   "CAPTURE_EPOCH", "E1")],
    )
    assert result.status == CoverageStatus.COMPLETE
    assert result.gap_ms == 0
    assert result.unknown_ms == 0


def test_partitioned_full_coverage_rounds_once_and_stays_complete():
    required_end = T0 + timedelta(seconds=41, milliseconds=641)
    result = CoverageCalculator.calculate(
        required_start=T0,
        required_end=required_end,
        evidence=[
            EvidenceInterval(T0, required_end, CoverageIntervalType.COVERED, "CAPTURE_READY", "A"),
            EvidenceInterval(
                T0 + timedelta(seconds=10, microseconds=478408),
                T0 + timedelta(seconds=32, microseconds=107865),
                CoverageIntervalType.COVERED,
                "REAL_PCM_TX_UDP_PCAP",
                "B",
            ),
        ],
    )
    assert result.required_ms == 41641
    assert result.covered_ms == 41641
    assert result.gap_ms == 0
    assert result.unknown_ms == 0
    assert result.status == CoverageStatus.COMPLETE


def test_confirmed_gap_downgrades_to_partial():
    result = CoverageCalculator.calculate(
        required_start=T0, required_end=T0 + timedelta(seconds=10),
        evidence=[
            EvidenceInterval(T0, T0 + timedelta(seconds=10), CoverageIntervalType.COVERED, "EPOCH", "E1"),
            EvidenceInterval(T0 + timedelta(seconds=4), T0 + timedelta(seconds=5), CoverageIntervalType.GAP,
                             "CAPTURE_GAP", "G1", "CONFIRMED"),
        ],
    )
    assert result.status == CoverageStatus.PARTIAL
    assert result.gap_ms == 1000


def test_unknown_gap_boundary_cannot_be_complete_even_with_epoch_coverage():
    result = CoverageCalculator.calculate(
        required_start=T0, required_end=T0 + timedelta(seconds=10),
        evidence=[EvidenceInterval(T0, T0 + timedelta(seconds=10), CoverageIntervalType.COVERED, "EPOCH", "E1")],
        uncertain_boundary=True,
    )
    assert result.status == CoverageStatus.PARTIAL
    assert "UNCERTAIN_GAP_BOUNDARY" in result.reasons


def test_no_evidence_is_failed_not_partial():
    result = CoverageCalculator.calculate(
        required_start=T0, required_end=T0 + timedelta(seconds=10), evidence=[]
    )
    assert result.status == CoverageStatus.FAILED


def test_kernel_capture_drop_is_an_uncertain_capture_boundary_not_complete():
    # Aggregate tcpdump kernel-drop count cannot identify the exact lost interval;
    # the deterministic policy therefore represents it as an uncertain boundary.
    result = CoverageCalculator.calculate(
        required_start=T0, required_end=T0 + timedelta(seconds=10),
        evidence=[EvidenceInterval(T0, T0 + timedelta(seconds=10), CoverageIntervalType.COVERED,
                                   "CAPTURE_EPOCH", "E1")],
        uncertain_boundary=True,
    )
    assert result.status == CoverageStatus.PARTIAL


def _pcap_builder_factory():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from app.db.base import Base
    from app.db import models as _existing_models  # noqa
    from app.capture_v2.db_models import CaptureEpoch, CaptureGap, CaptureSegment, CaptureSession
    from app.capture_v2.enums import CaptureHealth, CaptureSessionState
    engine = create_engine('sqlite+pysqlite:///:memory:', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[CaptureSession.__table__, CaptureEpoch.__table__, CaptureGap.__table__, CaptureSegment.__table__])
    F = sessionmaker(bind=engine, expire_on_commit=False)
    with F() as db, db.begin():
        db.add(CaptureSession(id='S', reproduction_session_id='R', device_id='D',
            state=CaptureSessionState.PREPARING.value, health_status=CaptureHealth.HEALTHY.value,
            capture_profile_id='p', capture_profile_version='1', platform_profile_id='mt7621',
            platform_profile_version='1', effective_profile={}))
    return F


def test_pcap_builder_marks_kernel_drop_uncertain():
    from app.capture_v2.coverage.pcap_source import PcapCoverageEvidenceBuilder
    from app.capture_v2.db_models import CaptureEpoch
    F = _pcap_builder_factory()
    with F() as db, db.begin():
        db.add(CaptureEpoch(id='E', capture_session_id='S', device_id='D', epoch_index=1,
            epoch_token='CAP_E', lease_epoch_started=1, state='ENDED', started_at=T0,
            ended_at=T0 + timedelta(seconds=10), packets_dropped_kernel=2))
    _evidence, uncertain, reasons = PcapCoverageEvidenceBuilder(F).build(
        capture_session_id='S', required_start=T0, required_end=T0 + timedelta(seconds=10))
    assert uncertain is True
    assert 'KERNEL_CAPTURE_DROP' in reasons


def test_pcap_builder_does_not_treat_remote_deleted_segment_with_missing_server_copy_as_durable():
    from app.capture_v2.coverage.pcap_source import PcapCoverageEvidenceBuilder
    from app.capture_v2.db_models import CaptureEpoch, CaptureSegment
    F = _pcap_builder_factory()
    with F() as db, db.begin():
        db.add(CaptureEpoch(id='E', capture_session_id='S', device_id='D', epoch_index=1,
            epoch_token='CAP_E', lease_epoch_started=1, state='ENDED', started_at=T0,
            ended_at=T0 + timedelta(seconds=10), packets_dropped_kernel=0))
        db.add(CaptureSegment(id='SEG', capture_session_id='S', capture_epoch_id='E', device_id='D',
            segment_seq=1, remote_path='/tmp/seg.pcap', remote_inode=1, remote_size=24,
            state='REMOTE_DELETED', storage_key='capture-v2/D/E/seg.pcap', server_size=24,
            sha256='0'*64, last_error_code='SERVER_COPY_MISSING'))
    _evidence, uncertain, reasons = PcapCoverageEvidenceBuilder(F).build(
        capture_session_id='S', required_start=T0, required_end=T0 + timedelta(seconds=10))
    assert uncertain is True
    assert 'SEGMENT_NOT_DURABLE' in reasons
