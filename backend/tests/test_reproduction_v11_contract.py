from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.contracts.enums import ArmValidationStatus, CallVerdict, CaptureChannel, ChannelHealth
from app.db.base import Base
from app.db.models import (
    ArmValidationResult, CaptureChannelHealth, Case, CaseDevice, ReproductionSession,
)
from app.reproduction.barriers import ArmReadinessBarrier
from app.reproduction.bundle import build_reproduction_evidence_bundle
from app.reproduction.pcap_codec import PcapRecord, build_pcap, build_rtp, udp_ethernet_frame
from app.reproduction.profile import ReproductionProfileRegistry
from app.reproduction.quick import EvidenceBackedCallQuickAnalyzer, QuickAnalysisInput
from app.reproduction.signal_observer import binding_relative_ms, observe_pcap_signals
from app.workers.reproduction_event_tasks import _binding_precedes_end, _ring_segment_retainable


def _write_pcap(path: Path, records: list[PcapRecord]) -> Path:
    path.write_bytes(build_pcap(records))
    return path


def test_pcm_proves_data_plane_but_never_binds_a_call(tmp_path: Path):
    path = _write_pcap(tmp_path / "pcm-only.pcap", [
        PcapRecord(1.0, udp_ethernet_frame("192.0.2.1", "192.0.2.2", 40000, 41000, b"rx")),
        PcapRecord(1.1, udp_ethernet_frame("192.0.2.1", "192.0.2.2", 50000, 51000, b"tx")),
    ])

    observed = observe_pcap_signals(path)

    assert observed.pcm_stream_verified is True
    assert observed.call_binding_event is None
    assert observed.external_call_ref is None


def test_header_only_pcap_is_not_retainable_ring_evidence(tmp_path: Path):
    empty = observe_pcap_signals(_write_pcap(tmp_path / "empty.pcap", []))
    one_packet = observe_pcap_signals(_write_pcap(tmp_path / "one.pcap", [
        PcapRecord(1.0, udp_ethernet_frame("192.0.2.1", "192.0.2.2", 40000, 41000, b"rx")),
    ]))

    assert empty.parse_error is None
    assert empty.udp_packets == 0
    assert _ring_segment_retainable(empty) is False
    assert _ring_segment_retainable(one_packet) is True


def test_sip_invite_is_the_preferred_call_binding_signal(tmp_path: Path):
    invite = (
        b"INVITE sip:1001@example.test SIP/2.0\r\n"
        b"Call-ID: deterministic-call-1\r\n"
        b"Content-Length: 0\r\n\r\n"
    )
    path = _write_pcap(tmp_path / "sip.pcap", [
        PcapRecord(2.0, udp_ethernet_frame("192.0.2.10", "192.0.2.20", 5060, 5060, invite)),
    ])

    observed = observe_pcap_signals(path)

    assert observed.call_binding_event == "SIP_INVITE"
    assert observed.external_call_ref == "deterministic-call-1"
    assert observed.call_binding_timestamp == 2.0


def test_progressing_rtp_is_a_reconstructable_fallback(tmp_path: Path):
    records = [
        PcapRecord(
            3.0 + index / 100,
            udp_ethernet_frame(
                "192.0.2.10",
                "192.0.2.20",
                12000,
                22000,
                build_rtp(b"voice", sequence=100 + index, timestamp=160 * index, ssrc=7),
            ),
        )
        for index in range(3)
    ]
    observed = observe_pcap_signals(_write_pcap(tmp_path / "rtp.pcap", records))

    assert observed.call_binding_event == "RTP_STREAM_START_FALLBACK"
    assert observed.rtp_packets == 3
    assert observed.external_call_ref == "rtp:192.0.2.10:12000>192.0.2.20:22000;ssrc=7"
    assert observed.call_binding_timestamp == 3.0
    assert binding_relative_ms(observed, segment_start_ms=12_000, segment_end_ms=17_000) == 12_000


def test_binding_timestamp_is_clamped_to_segment_bounds():
    from app.reproduction.signal_observer import CaptureSignalObservation

    observed = CaptureSignalObservation(first_timestamp=10.0, call_binding_timestamp=20.0)
    assert binding_relative_ms(observed, segment_start_ms=1_000, segment_end_ms=6_000) == 6_000


def test_generic_profile_does_not_treat_path_presence_as_a_fault():
    signal = QuickAnalysisInput(verdict=CallVerdict.INCONCLUSIVE)

    assert EvidenceBackedCallQuickAnalyzer._verdict(
        "VOIP_GENERIC_FULL_CAPTURE", {"ECHO_PATH", "DTMF_PATH"}, signal
    ) == CallVerdict.INCONCLUSIVE
    assert EvidenceBackedCallQuickAnalyzer._verdict(
        "ECHO", {"ECHO_PATH"}, signal
    ) == CallVerdict.MATCH


def test_dtmf_loss_requires_loss_signal_not_path_presence():
    """DTMF_PATH (dial digits / in-call key presses observed) must NOT be treated
    as DTMF loss. Only a real loss signal (PCM dial digits mismatch the SIP
    target) may produce a DTMF_LOSS MATCH. Regression test for the false MATCH
    observed on real hardware (call where 1234567890 was fully captured but the
    DTMF_LOSS profile still returned MATCH because DTMF_PATH was present)."""
    signal = QuickAnalysisInput(verdict=CallVerdict.INCONCLUSIVE)  # real-platform signal

    # DTMF observed but no loss -> NO_MATCH (was a false MATCH before the fix).
    assert EvidenceBackedCallQuickAnalyzer._verdict(
        "DTMF_LOSS", {"DTMF_PATH", "ACTIVE_MEDIA_WINDOW"}, signal
    ) == CallVerdict.NO_MATCH
    # Real loss signal present -> MATCH.
    assert EvidenceBackedCallQuickAnalyzer._verdict(
        "DTMF_LOSS", {"DTMF_PATH", "DTMF_LOSS"}, signal
    ) == CallVerdict.MATCH


def test_findings_dtmf_loss_only_from_dial_mismatch():
    """DTMF_PATH is an observation; DTMF_LOSS is added only when the cross-layer
    dial comparison reports a PCM-vs-SIP mismatch."""
    result = {
        'packet': {'calls': [], 'anomalies': [], 'rtp_streams': []},
        'correlations': [],
        'echo_paths': [],
        'periodic_interference_paths': [],
        'cross_layer_events': [{'type': 'DTMF_SIP_DIAL_MATCH'}],
        'pcm': {'streams': [{'sessions': [{'dtmf_sequences': [{'digits': '301'}]}]}]},
    }
    assert EvidenceBackedCallQuickAnalyzer._findings(result) == {'DTMF_PATH'}

    result['cross_layer_events'].append({'type': 'DTMF_SIP_DIAL_MISMATCH'})
    findings = EvidenceBackedCallQuickAnalyzer._findings(result)
    assert 'DTMF_PATH' in findings
    assert 'DTMF_LOSS' in findings


def test_pcm_dtmf_sequences_extracts_media_truth():
    """The authoritative PCM-media DTMF sequences (complete even when the FXS
    event report drops fast-pressed digits) are extracted from the analysis."""
    result = {
        'pcm': {'streams': [
            {'tap': {'name': 'pcm_rx'}, 'sessions': [
                {'session_index': 0, 'dtmf_sequences': [
                    {'digits': '301', 'start_seconds': 3.0, 'end_seconds': 3.68, 'event_count': 3, 'min_confidence': 0.8},
                    {'digits': '11110000', 'start_seconds': 9.0, 'end_seconds': 10.0, 'event_count': 8, 'min_confidence': 1.0},
                ]},
            ]},
            {'tap': {'name': 'pcm_tx'}, 'sessions': [
                {'session_index': 0, 'dtmf_sequences': []},
            ]},
        ]},
    }
    seqs = EvidenceBackedCallQuickAnalyzer._pcm_dtmf_sequences(result)
    assert len(seqs) == 2
    assert seqs[0]['digits'] == '301' and seqs[0]['tap'] == 'pcm_rx'
    assert seqs[1]['digits'] == '11110000' and seqs[1]['event_count'] == 8
    assert EvidenceBackedCallQuickAnalyzer._pcm_dtmf_sequences({'pcm': {'streams': []}}) == ()


def test_activity_gated_arm_accepts_path_ready_but_marks_verification_pending():
    root = Path(__file__).resolve().parents[2] / "profiles"
    profile = ReproductionProfileRegistry(root).get("AUDIO_NOISE").definition
    observed = {
        CaptureChannel.PCAP.value: {
            "status": ChannelHealth.HEALTHY.value,
            "pcap_header_valid": True,
            "packet_count": 0,
            "advancing": False,
        },
        CaptureChannel.PCM_RX.value: {
            "status": ChannelHealth.STARTING.value,
            "configured": True,
            "verification_pending": True,
            "packet_count": 0,
            "advancing": False,
        },
        CaptureChannel.PCM_TX.value: {
            "status": ChannelHealth.STARTING.value,
            "configured": True,
            "verification_pending": True,
            "packet_count": 0,
            "advancing": False,
        },
        CaptureChannel.DEBUG.value: {
            "status": ChannelHealth.HEALTHY.value,
            "enabled": True,
            "heartbeat": True,
        },
    }

    decision = ArmReadinessBarrier.evaluate(
        profile,
        observed,
        [CaptureChannel.PCAP, CaptureChannel.PCM_RX, CaptureChannel.PCM_TX, CaptureChannel.DEBUG],
    )

    assert decision.ready is True
    assert decision.status == ArmValidationStatus.PARTIAL
    assert decision.readiness_phase == "CAPTURE_PATH_READY"


def _activity_validation_fixture():
    engine = create_engine('sqlite+pysqlite:///:memory:')
    Base.metadata.create_all(engine)
    db = Session(engine)
    case = Case(case_no='V11-READINESS', summary='activity gated', status='ANALYZING')
    db.add(case); db.flush()
    device = CaseDevice(
        case_id=case.id, ip='192.0.2.10', ssh_port=22, sn='V11', username='admin')
    db.add(device); db.flush()
    session = ReproductionSession(
        case_id=case.id, device_id=device.id, profile_key='AUDIO_NOISE',
        profile_version='1.0.0', profile_checksum='a' * 64,
        effective_profile_snapshot={}, state='WATCHING', capture_stage='BASE',
        cleanup_required=True, cleanup_status='REQUIRED', capture_completeness='PARTIAL',
    )
    db.add(session); db.flush()
    return db, session


def test_activity_data_plane_success_is_persisted_as_arm_validation_evidence():
    db, session = _activity_validation_fixture()
    try:
        for channel in (CaptureChannel.PCM_RX, CaptureChannel.PCM_TX):
            db.add(CaptureChannelHealth(
                session_id=session.id, channel=channel.value,
                status=ChannelHealth.HEALTHY.value, packet_count=4,
                health_json={'verification_pending': True},
            ))
        db.flush()

        decision = ArmReadinessBarrier.persist_activity_data_plane_validation(
            db, session=session)
        db.flush()

        row = db.scalar(select(ArmValidationResult).where(
            ArmValidationResult.session_id == session.id))
        assert decision.ready is True
        assert decision.readiness_phase == 'DATA_PLANE_VERIFIED'
        assert row.status == ArmValidationStatus.PASSED.value
        assert row.observed_channels_json['_readiness_phase'] == 'DATA_PLANE_VERIFIED'
        bundle = build_reproduction_evidence_bundle(db, session)
        assert bundle['arm_validations'][0]['readiness_phase'] == 'DATA_PLANE_VERIFIED'
    finally:
        db.close()


def test_activity_data_plane_gap_names_missing_direction_and_requests_recovery():
    db, session = _activity_validation_fixture()
    try:
        db.add(CaptureChannelHealth(
            session_id=session.id, channel=CaptureChannel.PCM_RX.value,
            status=ChannelHealth.HEALTHY.value, packet_count=3,
            health_json={'verification_pending': True},
        ))
        db.add(CaptureChannelHealth(
            session_id=session.id, channel=CaptureChannel.PCM_TX.value,
            status=ChannelHealth.STARTING.value, packet_count=0,
            health_json={'verification_pending': True},
        ))
        db.flush()

        decision = ArmReadinessBarrier.persist_activity_data_plane_validation(
            db, session=session)
        db.flush()

        assert decision.ready is False
        assert decision.readiness_phase == 'CAPTURE_PATH_DEGRADED'
        assert decision.failed_reasons == ('PCM_TX_NOT_VERIFIED',)
        assert session.evidence_sufficiency == 'INSUFFICIENT_CAPTURE_RECOVERY'
        row = db.scalar(select(ArmValidationResult).where(
            ArmValidationResult.session_id == session.id))
        assert row.status == ArmValidationStatus.PARTIAL.value
        assert row.failed_reasons_json == ['PCM_TX_NOT_VERIFIED']
    finally:
        db.close()


def test_late_sip_invite_still_binds_a_short_call():
    """A SIP INVITE observed AFTER the FXS ONHOOK must still bind the Call.

    Short calls (a few seconds) expose a segment-download/analysis lag on real
    DUTs: the deterministic INVITE is captured in a segment whose observation
    completes 10+s after hangup (APF1250 eb9d7edb, APF3260 052884a5 - the call
    was made, media/INVITE exist, yet the attempt was wrongly INVALID). The
    INVITE is unambiguous call-setup evidence and is emitted *during* the call,
    so late observation must not discard it. Regression test for the real-device
    short-call binding fix.
    """
    # Late SIP INVITE (bind after ONHOOK) in the current attempt's segment.
    assert _binding_precedes_end(
        binding_event='SIP_INVITE',
        bind_ms=50_175,          # observed segment anchor, after ONHOOK
        pending_onhook_ms=38_593,
        segment_start_ms=32_651,
        attempt_start_ms=31_422,
    ) is True

    # RTP-only fallback keeps the strict ordering (tail-packet guard).
    assert _binding_precedes_end(
        binding_event='RTP_STREAM_START_FALLBACK',
        bind_ms=50_175,
        pending_onhook_ms=38_593,
        segment_start_ms=32_651,
        attempt_start_ms=31_422,
    ) is False

    # A stale INVITE segment from the PREVIOUS call (start before the current
    # attempt's OFFHOOK) must not bind to the fresh attempt.
    assert _binding_precedes_end(
        binding_event='SIP_INVITE',
        bind_ms=50_175,
        pending_onhook_ms=38_593,
        segment_start_ms=20_000,   # older than the attempt's OFFHOOK
        attempt_start_ms=31_422,
    ) is False

    # When segment/attempt anchors are unknown, a SIP INVITE is still trusted.
    assert _binding_precedes_end(
        binding_event='SIP_INVITE', bind_ms=50_175, pending_onhook_ms=38_593,
    ) is True

    # With no pending ONHOOK, RTP fallback is allowed regardless of order.
    assert _binding_precedes_end(
        binding_event='RTP_STREAM_START_FALLBACK',
        bind_ms=50_175, pending_onhook_ms=None,
    ) is True
