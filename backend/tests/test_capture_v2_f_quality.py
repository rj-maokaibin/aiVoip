from app.capture_v2.enums import CaptureCompleteness, DiagnosticConfidence, SignalAvailabilityStatus
from app.capture_v2.quality.confidence import ConfidenceInput, DiagnosticConfidenceEvaluator
from app.capture_v2.quality.dtmf_fusion import DtmfFusion, DtmfSource
from app.capture_v2.quality.signals import SignalAvailabilityEvaluator, SignalEvidence


def test_capture_complete_but_sip_tls_is_unavailable_encrypted_not_capture_partial():
    sip = SignalAvailabilityEvaluator.evaluate(SignalEvidence(
        channel="SIP", expected=True, captured=True, usable=False, encrypted=True
    ))
    assert sip.availability == SignalAvailabilityStatus.UNAVAILABLE_ENCRYPTED
    decision = DiagnosticConfidenceEvaluator.evaluate(ConfidenceInput(
        capture_completeness=CaptureCompleteness.COMPLETE,
        signal_availability={"SIP": sip.availability},
        required_channels_for_diagnosis=("SIP",), independent_support_count=2,
    ))
    assert decision.confidence == DiagnosticConfidence.LOW


def test_media_root_cause_can_be_high_without_sip_when_sip_not_required():
    availability = {"RTP": SignalAvailabilityStatus.AVAILABLE, "PCM_RX": SignalAvailabilityStatus.AVAILABLE}
    decision = DiagnosticConfidenceEvaluator.evaluate(ConfidenceInput(
        capture_completeness=CaptureCompleteness.COMPLETE,
        signal_availability=availability,
        required_channels_for_diagnosis=("RTP", "PCM_RX"), independent_support_count=2,
    ))
    assert decision.confidence == DiagnosticConfidence.HIGH


def test_partial_capture_caps_confidence_at_medium():
    decision = DiagnosticConfidenceEvaluator.evaluate(ConfidenceInput(
        capture_completeness=CaptureCompleteness.PARTIAL,
        signal_availability={"RTP": SignalAvailabilityStatus.AVAILABLE},
        required_channels_for_diagnosis=("RTP",), independent_support_count=3,
    ))
    assert decision.confidence == DiagnosticConfidence.MEDIUM


def test_dtmf_fusion_finds_layer_divergence():
    result = DtmfFusion.fuse(
        DtmfSource("FXS", "123467890"),
        DtmfSource("CALL_MANAGER", "123467890"),
        DtmfSource("SIP_URI", "123467890"),
        DtmfSource("PCM", "1234567890"),
    )
    assert result.status == "DIVERGENT"
    assert result.consensus == "123467890"
    pcm = [m for m in result.mismatches if m["source"] == "PCM"][0]
    assert pcm["first_divergence_index"] == 4
