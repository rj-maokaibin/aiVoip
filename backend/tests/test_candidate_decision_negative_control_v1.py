from __future__ import annotations

from app.analyzers.audio.candidate_decision import (
    INCONCLUSIVE,
    PROMOTED,
    SUPPRESSED,
    decide_click_pop,
    decide_raw_click_pop,
    decide_silence,
)
from app.reports.candidate_decision import decision_summary, resolve_candidate_decisions
from app.reports.finding_composer import build_normal_evidence, compose_findings


FIELD_CLICK_TIME = 1786690964.332755
FIELD_DTMF_START = 1786690964.323755


def test_field_click_candidate_9ms_after_dtmf_start_is_suppressed():
    candidate = {
        "type": "CLICK_POP",
        "time_seconds": FIELD_CLICK_TIME - FIELD_DTMF_START,
        "confidence": 0.95,
        "jump": 12000,
        "energy_rise_db": 15.0,
        "highband_energy_ratio": 0.30,
    }
    decision = decide_raw_click_pop(
        candidate,
        [{"digit": "6", "start_seconds": 0.0, "end_seconds": 0.14, "confidence": 0.99}],
        scope={"pcm_tap": "pcm_rx", "pcm_session_index": 0},
    )

    assert decision["status"] == SUPPRESSED
    assert decision["reason_code"] == "NEGCTRL_DTMF_TRANSIENT"
    assert decision["negative_controls"][0]["digit"] == "6"


def test_active_media_click_overlapping_dtmf_is_suppressed():
    decision = decide_click_pop(
        {"confidence": 0.95, "jump": 12000, "energy_rise_db": 14.0, "highband_energy_ratio": 0.25},
        absolute_time=100.109,
        scope={"pcm_tap": "pcm_rx", "pcm_session_index": 0, "call_id": "sip-call"},
        dtmf_intervals=[{"digit": "5", "start": 100.100, "end": 100.220}],
        media_start=99.0,
        media_end=110.0,
    )

    assert decision["status"] == SUPPRESSED
    assert decision["reason_code"] == "NEGCTRL_DTMF_TRANSIENT"


def test_active_media_click_near_call_boundary_is_suppressed():
    decision = decide_click_pop(
        {"confidence": 0.95, "jump": 12000, "energy_rise_db": 14.0, "highband_energy_ratio": 0.25},
        absolute_time=100.050,
        scope={"pcm_tap": "pcm_rx", "pcm_session_index": 0},
        dtmf_intervals=[],
        media_start=100.0,
        media_end=110.0,
    )

    assert decision["status"] == SUPPRESSED
    assert decision["reason_code"] == "NEGCTRL_MEDIA_BOUNDARY_TRANSIENT"


def test_click_clearing_negative_controls_and_confidence_gate_is_promoted():
    decision = decide_click_pop(
        {"confidence": 0.91, "jump": 14000, "energy_rise_db": 16.0, "highband_energy_ratio": 0.32},
        absolute_time=105.0,
        scope={"pcm_tap": "pcm_rx", "pcm_session_index": 0},
        dtmf_intervals=[],
        media_start=100.0,
        media_end=110.0,
    )

    assert decision["status"] == PROMOTED
    assert decision["reason_code"] == "CLICK_POP_NEGATIVE_CONTROLS_CLEARED"


def test_low_confidence_click_stays_inconclusive():
    decision = decide_click_pop(
        {"confidence": 0.50, "jump": 7000, "energy_rise_db": 8.0, "highband_energy_ratio": 0.09},
        absolute_time=105.0,
        scope={"pcm_tap": "pcm_rx", "pcm_session_index": 0},
        dtmf_intervals=[],
        media_start=100.0,
        media_end=110.0,
    )

    assert decision["status"] == INCONCLUSIVE
    assert decision["reason_code"] == "CLICK_POP_CONFIDENCE_BELOW_PROMOTION_GATE"


def test_pcm_tx_silence_matching_correlated_rtp_source_silence_is_suppressed():
    decision = decide_silence(
        {"duration_ms": 800, "median_dbfs": -120.0},
        absolute_start=102.0,
        absolute_end=102.8,
        scope={"pcm_tap": "pcm_tx", "pcm_session_index": 0},
        counterpart_stream_id="rtp-down",
        counterpart_correlation=0.91,
        counterpart_metrics={
            "event_rms_dbfs": -70.0,
            "pre_context_rms_dbfs": -28.0,
            "post_context_rms_dbfs": -27.0,
            "context_peak_dbfs": -27.0,
        },
    )

    assert decision["status"] == SUPPRESSED
    assert decision["reason_code"] == "NEGCTRL_MATCHED_RTP_SOURCE_SILENCE"


def test_pcm_silence_with_active_correlated_rtp_counterpart_is_promoted():
    decision = decide_silence(
        {"duration_ms": 500, "median_dbfs": -120.0},
        absolute_start=102.0,
        absolute_end=102.5,
        scope={"pcm_tap": "pcm_tx", "pcm_session_index": 0},
        counterpart_stream_id="rtp-down",
        counterpart_correlation=0.92,
        counterpart_metrics={
            "event_rms_dbfs": -30.0,
            "pre_context_rms_dbfs": -31.0,
            "post_context_rms_dbfs": -30.0,
            "context_peak_dbfs": -30.0,
        },
    )

    assert decision["status"] == PROMOTED
    assert decision["reason_code"] == "CROSS_LAYER_SILENCE_MISMATCH_CONFIRMED"


def test_silence_without_counterpart_window_metrics_stays_inconclusive():
    decision = decide_silence(
        {"duration_ms": 500, "median_dbfs": -120.0},
        absolute_start=102.0,
        absolute_end=102.5,
        scope={"pcm_tap": "pcm_tx", "pcm_session_index": 0},
        counterpart_stream_id="rtp-down",
        counterpart_correlation=0.92,
        counterpart_metrics=None,
    )

    assert decision["status"] == INCONCLUSIVE


def _legacy_media_with_matched_silence() -> dict:
    return {
        "degraded_reason": None,
        "pcm": {
            "streams": [{
                "tap": {"name": "pcm_tx", "direction": "TX"},
                "sessions": [{"session_index": 0, "start_time": 100.0, "dtmf_events": []}],
            }],
        },
        "correlations": [{
            "type": "PCM_RTP_CORRELATION",
            "details": {
                "pcm_tap": "pcm_tx",
                "pcm_session_index": 0,
                "rtp_stream_id": "rtp-down",
                "correlation": {"absolute_correlation": 0.91, "quality": "HIGH"},
            },
        }],
        "rtp_audio_tracks": [{
            "stream_id": "rtp-down",
            "start_time": 100.0,
            "silence_events": [{
                "start_seconds": 2.0,
                "end_seconds": 2.8,
                "duration_ms": 800,
                "median_dbfs": -70.0,
                "pre_context_dbfs": -28.0,
                "post_context_dbfs": -27.0,
            }],
        }],
        "cross_layer_events": [{
            "type": "UNEXPECTED_SILENCE",
            "time": 102.0,
            "severity": "MEDIUM",
            "evidence_level": "L2",
            "scope": {
                "call_id": "sip-call",
                "pcm_tap": "pcm_tx",
                "pcm_session_index": 0,
                "active_media_window": {"start_time": 100.0, "end_time": 110.0},
            },
            "details": {
                "start_seconds": 2.0,
                "end_seconds": 2.8,
                "duration_ms": 800,
                "median_dbfs": -120.0,
                "absolute_start_time": 102.0,
            },
        }],
        "periodic_interference_paths": [],
    }


def test_legacy_pcm_tx_silence_is_rechecked_and_suppressed_before_report_finding():
    media = _legacy_media_with_matched_silence()
    resolved = resolve_candidate_decisions(media)
    findings = compose_findings(media=media)

    assert resolved["suppressed"] == 1
    assert resolved["promoted"] == 0
    assert all(f["type"] != "UNEXPECTED_SILENCE" for f in findings)
    normal = build_normal_evidence(None, None, media)
    assert any(x["type"] == "AUDIO_CANDIDATES_SUPPRESSED_BY_NEGATIVE_CONTROL" for x in normal)


def test_raw_pcm_candidate_without_explicit_promotion_never_becomes_finding():
    pcm = {
        "streams": [{
            "tap": {"name": "pcm_rx", "direction": "RX"},
            "sessions": [{
                "session_index": 0,
                "start_time": 100.0,
                "end_time": 110.0,
                "gap_events": [],
                "hum": {"level": "LOW"},
                "silence_events": [{"start_seconds": 1.0, "end_seconds": 2.0, "duration_ms": 1000}],
                "click_pop_events": [{"time_seconds": 3.0, "confidence": 0.95}],
                "dtmf_quality_events": [],
            }],
        }],
    }

    findings = compose_findings(pcm=pcm)
    assert all(f["type"] not in {"UNEXPECTED_SILENCE", "CLICK_POP"} for f in findings)


def test_candidate_decision_summary_is_auditable_by_reason_and_type():
    summary = decision_summary(_legacy_media_with_matched_silence())

    assert summary["candidate_count"] == 1
    assert summary["suppressed"] == 1
    assert summary["by_type"]["UNEXPECTED_SILENCE"][SUPPRESSED] == 1
    assert summary["by_reason"]["NEGCTRL_MATCHED_RTP_SOURCE_SILENCE"] == 1
