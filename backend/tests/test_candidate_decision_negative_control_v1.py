from copy import deepcopy

from app.analyzers.media.candidate_decision import (
    INCONCLUSIVE,
    PROMOTED,
    REJECTED_NEGATIVE_CONTROL,
    apply_candidate_decisions,
    decide_event,
)
from app.reports.finding_composer import compose_findings


def _base_media() -> dict:
    return {
        "summary": {},
        "packet": {
            "calls": [{"call_id": "sip-call-1", "media_start_time": 110.0, "media_end_time": 150.0}],
        },
        "pcm": {
            "streams": [
                {
                    "tap": {"name": "pcm_rx", "direction": "RX"},
                    "sessions": [{
                        "session_index": 0,
                        "start_time": 100.0,
                        "end_time": 160.0,
                        "dtmf_events": [{
                            "digit": "6",
                            "start_seconds": 14.320,
                            "end_seconds": 14.460,
                            "duration_ms": 140.0,
                            "confidence": 0.99,
                        }],
                        "click_pop_events": [],
                        "silence_events": [],
                    }],
                },
                {
                    "tap": {"name": "pcm_tx", "direction": "TX"},
                    "sessions": [{
                        "session_index": 0,
                        "start_time": 100.0,
                        "end_time": 160.0,
                        "dtmf_events": [],
                        "click_pop_events": [],
                        "silence_events": [],
                    }],
                },
            ]
        },
        "rtp_audio_tracks": [],
        "correlations": [],
        "active_media_audio_events": [],
        "cross_layer_events": [],
    }


def test_click_pop_overlapping_dtmf_is_rejected_negative_control():
    media = _base_media()
    event = {
        "type": "CLICK_POP",
        "time": 114.332755,
        "severity": "MEDIUM",
        "evidence_level": "L3",
        "scope": {
            "call_id": "sip-call-1",
            "pcm_tap": "pcm_rx",
            "pcm_session_index": 0,
            "active_media_window": {"start_time": 110.0, "end_time": 150.0},
        },
        "details": {"confidence": 0.91},
    }

    decision = decide_event(media, event)

    assert decision["status"] == REJECTED_NEGATIVE_CONTROL
    assert decision["reason_code"] == "DTMF_OVERLAP"
    assert decision["negative_controls"][0]["digit"] == "6"


def test_click_pop_at_media_boundary_is_rejected_transient():
    media = _base_media()
    event = {
        "type": "CLICK_POP",
        "time": 110.050,
        "scope": {
            "call_id": "sip-call-1",
            "pcm_tap": "pcm_rx",
            "pcm_session_index": 0,
            "active_media_window": {"start_time": 110.0, "end_time": 150.0},
        },
        "details": {"confidence": 0.9},
    }

    decision = decide_event(media, event)

    assert decision["status"] == REJECTED_NEGATIVE_CONTROL
    assert decision["reason_code"] == "MEDIA_BOUNDARY_TRANSIENT"


def test_click_pop_inside_active_media_without_negative_control_can_promote():
    media = _base_media()
    event = {
        "type": "CLICK_POP",
        "time": 130.0,
        "severity": "MEDIUM",
        "evidence_level": "L3",
        "scope": {
            "call_id": "sip-call-1",
            "pcm_tap": "pcm_rx",
            "pcm_session_index": 0,
            "active_media_window": {"start_time": 110.0, "end_time": 150.0},
        },
        "details": {"confidence": 0.95},
    }

    decision = decide_event(media, event)

    assert decision["status"] == PROMOTED
    assert decision["reason_code"] == "ACTIVE_MEDIA_MULTI_FEATURE_CLICK"


def test_pcm_tx_silence_matching_correlated_rtp_silence_is_rejected():
    media = _base_media()
    event = {
        "type": "UNEXPECTED_SILENCE",
        "time": 125.0,
        "severity": "MEDIUM",
        "evidence_level": "L2",
        "scope": {
            "call_id": "sip-call-1",
            "pcm_tap": "pcm_tx",
            "pcm_session_index": 0,
            "active_media_window": {"start_time": 110.0, "end_time": 150.0},
        },
        "details": {"duration_ms": 1000.0, "absolute_start_time": 125.0, "absolute_end_time": 126.0},
    }
    media["correlations"] = [{
        "type": "PCM_RTP_CORRELATION",
        "details": {
            "pcm_tap": "pcm_tx",
            "pcm_session_index": 0,
            "rtp_stream_id": "downstream",
            "correlation": {"absolute_correlation": 0.88, "quality": "HIGH"},
        },
    }]
    media["rtp_audio_tracks"] = [{
        "stream_id": "downstream",
        "start_time": 120.0,
        "end_time": 150.0,
        "silence_events": [{"start_seconds": 4.8, "end_seconds": 6.2, "duration_ms": 1400.0}],
    }]

    decision = decide_event(media, event)

    assert decision["status"] == REJECTED_NEGATIVE_CONTROL
    assert decision["reason_code"] == "RTP_COUNTERPART_SILENCE"
    assert decision["negative_controls"][0]["overlap_ratio"] >= 0.9


def test_pcm_silence_without_positive_counterpart_activity_proof_is_inconclusive_not_finding():
    media = _base_media()
    event = {
        "type": "UNEXPECTED_SILENCE",
        "time": 125.0,
        "severity": "MEDIUM",
        "evidence_level": "L2",
        "scope": {
            "call_id": "sip-call-1",
            "pcm_tap": "pcm_tx",
            "pcm_session_index": 0,
            "active_media_window": {"start_time": 110.0, "end_time": 150.0},
        },
        "details": {"duration_ms": 800.0, "absolute_start_time": 125.0, "absolute_end_time": 125.8},
    }
    media["correlations"] = [{
        "type": "PCM_RTP_CORRELATION",
        "details": {
            "pcm_tap": "pcm_tx",
            "pcm_session_index": 0,
            "rtp_stream_id": "downstream",
            "correlation": {"absolute_correlation": 0.90, "quality": "HIGH"},
        },
    }]
    media["rtp_audio_tracks"] = [{"stream_id": "downstream", "start_time": 120.0, "end_time": 150.0, "silence_events": []}]

    decision = decide_event(media, event)

    assert decision["status"] == INCONCLUSIVE
    assert decision["reason_code"] == "COUNTERPART_ACTIVITY_NOT_PROVEN"


def test_raw_pcm_click_and_silence_candidates_are_not_direct_report_findings():
    media = _base_media()
    pcm = deepcopy(media["pcm"])
    rx = pcm["streams"][0]["sessions"][0]
    rx["click_pop_events"] = [{"time_seconds": 14.332755, "confidence": 0.92}]
    tx = pcm["streams"][1]["sessions"][0]
    tx["silence_events"] = [{"start_seconds": 25.0, "end_seconds": 26.0, "duration_ms": 1000.0}]
    media["pcm"] = deepcopy(pcm)
    media["active_media_audio_events"] = []
    media["cross_layer_events"] = []
    results = {"pcm_intelligence": pcm, "media_intelligence": media, "packet_intelligence": None}

    normalized = apply_candidate_decisions(results)
    findings = compose_findings(
        packet=None,
        pcm=normalized["pcm_intelligence"],
        media=normalized["media_intelligence"],
        source_run_ids={"pcm_intelligence": "pcm-run", "media_intelligence": "media-run"},
    )

    assert not any(f["type"] in {"CLICK_POP", "UNEXPECTED_SILENCE"} for f in findings)
    assert normalized["pcm_intelligence"]["streams"][0]["sessions"][0]["click_pop_candidates"]
    assert normalized["pcm_intelligence"]["streams"][1]["sessions"][0]["silence_candidates"]


def test_only_promoted_media_click_reaches_finding_composer():
    media = _base_media()
    rejected = {
        "type": "CLICK_POP",
        "time": 114.332755,
        "severity": "MEDIUM",
        "evidence_level": "L3",
        "scope": {"call_id": "sip-call-1", "pcm_tap": "pcm_rx", "pcm_session_index": 0,
                  "active_media_window": {"start_time": 110.0, "end_time": 150.0}},
        "details": {"confidence": 0.9},
    }
    promoted = {
        "type": "CLICK_POP",
        "time": 130.0,
        "severity": "MEDIUM",
        "evidence_level": "L3",
        "scope": {"call_id": "sip-call-1", "pcm_tap": "pcm_rx", "pcm_session_index": 0,
                  "active_media_window": {"start_time": 110.0, "end_time": 150.0}},
        "details": {"confidence": 0.95},
    }
    media["active_media_audio_events"] = [rejected, promoted]
    media["cross_layer_events"] = [rejected, promoted]
    results = {"pcm_intelligence": deepcopy(media["pcm"]), "media_intelligence": media, "packet_intelligence": None}

    normalized = apply_candidate_decisions(results)
    findings = compose_findings(
        packet=None,
        pcm=normalized["pcm_intelligence"],
        media=normalized["media_intelligence"],
        source_run_ids={"pcm_intelligence": "pcm-run", "media_intelligence": "media-run"},
    )

    click_findings = [f for f in findings if f["type"] == "CLICK_POP"]
    assert len(click_findings) == 1
    assert click_findings[0]["time_range"]["start"] == 130.0
    statuses = {d["reason_code"]: d["status"] for d in normalized["media_intelligence"]["candidate_decisions"]}
    assert statuses["DTMF_OVERLAP"] == REJECTED_NEGATIVE_CONTROL
    assert statuses["ACTIVE_MEDIA_MULTI_FEATURE_CLICK"] == PROMOTED
