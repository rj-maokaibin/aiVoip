import numpy as np

from app.analyzers.audio.rtp_audio import _energy_timeline
from app.analyzers.media.candidate_decision import (
    PROMOTED,
    REJECTED_NEGATIVE_CONTROL,
    apply_candidate_decisions,
    decide_event,
)


def _silence_event() -> dict:
    return {
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


def _media_with_track(track: dict) -> dict:
    return {
        "summary": {},
        "packet": {"calls": [{"call_id": "sip-call-1", "media_start_time": 110.0, "media_end_time": 150.0}]},
        "pcm": {"streams": [{"tap": {"name": "pcm_tx", "direction": "TX"}, "sessions": [{
            "session_index": 0, "start_time": 100.0, "end_time": 160.0,
            "dtmf_events": [], "click_pop_events": [], "silence_events": [],
        }]}]},
        "correlations": [{"type": "PCM_RTP_CORRELATION", "details": {
            "pcm_tap": "pcm_tx", "pcm_session_index": 0, "rtp_stream_id": "downstream",
            "correlation": {"absolute_correlation": 0.90, "quality": "HIGH"},
        }}],
        "rtp_audio_tracks": [track],
        "active_media_audio_events": [],
        "cross_layer_events": [],
    }


def test_rtp_energy_timeline_exposes_window_dbfs_and_threshold():
    samples = np.full(8000, 6000, dtype=np.int16)
    timeline = _energy_timeline(samples, 8000, frame_ms=100)

    assert timeline["frame_ms"] == 100
    assert len(timeline["windows"]) == 10
    assert timeline["threshold_dbfs"] < 0
    assert all(w["rms_dbfs"] > -30 for w in timeline["windows"])


def test_pcm_silence_promotes_only_when_correlated_rtp_same_window_is_proven_active():
    samples = np.full(8000 * 10, 7000, dtype=np.int16)
    track = {
        "stream_id": "downstream",
        "start_time": 120.0,
        "end_time": 130.0,
        "silence_events": [],
        "energy_timeline": _energy_timeline(samples, 8000, frame_ms=100),
    }
    media = _media_with_track(track)

    decision = decide_event(media, _silence_event())

    assert decision["status"] == PROMOTED
    assert decision["reason_code"] == "CROSS_LAYER_SILENCE_MISMATCH"
    assert decision["positive_evidence"]["active_ratio"] >= 0.9
    assert decision["promoted_event"]["details"]["candidate_decision"]["status"] == PROMOTED


def test_pcm_silence_rejected_when_correlated_rtp_same_window_is_low_energy():
    samples = np.zeros(8000 * 10, dtype=np.int16)
    track = {
        "stream_id": "downstream",
        "start_time": 120.0,
        "end_time": 130.0,
        "silence_events": [],
        "energy_timeline": _energy_timeline(samples, 8000, frame_ms=100),
    }
    media = _media_with_track(track)

    decision = decide_event(media, _silence_event())

    assert decision["status"] == REJECTED_NEGATIVE_CONTROL
    assert decision["reason_code"] == "RTP_COUNTERPART_LOW_ENERGY"


def test_media_unavailable_keeps_raw_pcm_candidates_auditable_but_not_promotable():
    pcm = {"summary": {}, "streams": [{"tap": {"name": "pcm_rx"}, "sessions": [{
        "session_index": 0,
        "click_pop_events": [{"time_seconds": 1.0, "confidence": 0.9}],
        "silence_events": [{"start_seconds": 2.0, "end_seconds": 3.0, "duration_ms": 1000.0}],
    }]}]}
    results = {"pcm_intelligence": pcm, "media_intelligence": None, "packet_intelligence": None}

    normalized = apply_candidate_decisions(results)
    session = normalized["pcm_intelligence"]["streams"][0]["sessions"][0]

    assert session["click_pop_events"] == []
    assert session["silence_events"] == []
    assert session["click_pop_candidates"]
    assert session["silence_candidates"]
    assert normalized["pcm_intelligence"]["summary"]["candidate_decision"]["reason_code"] == "MEDIA_ANALYZER_UNAVAILABLE"
