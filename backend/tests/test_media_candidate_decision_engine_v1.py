from __future__ import annotations

import numpy as np

import app.analyzers.media.engine as media_module
from app.analyzers.audio.rtp_audio import RenderedRtpTrack
from app.analyzers.media.engine import MediaIntelligenceEngine
from app.analyzers.profile import get_default_analyzer_profile


def _engine() -> MediaIntelligenceEngine:
    engine = object.__new__(MediaIntelligenceEngine)
    engine.analyzer_profile = get_default_analyzer_profile()
    return engine


def _pcm_signal(samples: np.ndarray) -> dict:
    return {
        "tap": {"name": "pcm_tx", "direction": "TX"},
        "session_index": 0,
        "start_time": 100.0,
        "end_time": 110.0,
        "samples": samples,
        "sample_rate": 8000,
    }


def _packet_result() -> dict:
    return {
        "calls": [{
            "call_id": "sip-call",
            "media_start_time": 100.0,
            "media_end_time": 110.0,
        }],
    }


def _pcm_result(*, dtmf_events=None) -> dict:
    return {
        "streams": [{
            "tap": {"name": "pcm_tx", "direction": "TX"},
            "sessions": [{
                "session_index": 0,
                "start_time": 100.0,
                "dtmf_events": dtmf_events or [],
            }],
        }],
    }


def _track(
    samples: np.ndarray,
    *,
    inserted_loss_samples: int = 0,
    missing_payload_packets: int = 0,
) -> RenderedRtpTrack:
    return RenderedRtpTrack(
        stream_id="rtp-down",
        src_ip="192.168.3.200",
        src_port=11446,
        dst_ip="192.168.150.4",
        dst_port=10000,
        ssrc=1234,
        codec="PCMU",
        sample_rate=8000,
        channels=1,
        start_time=100.0,
        end_time=110.0,
        samples=samples,
        packet_count=500,
        inserted_loss_samples=inserted_loss_samples,
        missing_payload_packets=missing_payload_packets,
        sequence_first=1,
        sequence_last=500,
    )


def _correlations(score: float = 0.95, *, lag_ms: float = 0.0) -> list[dict]:
    return [{
        "type": "PCM_RTP_CORRELATION",
        "details": {
            "pcm_tap": "pcm_tx",
            "pcm_session_index": 0,
            "rtp_stream_id": "rtp-down",
            "correlation": {
                "absolute_correlation": score,
                "quality": "HIGH" if score >= 0.8 else "MEDIUM",
                "lag_ms": lag_ms,
            },
        },
    }]


def _silence_candidate() -> dict:
    return {
        "start_seconds": 2.0,
        "end_seconds": 2.5,
        "duration_ms": 500.0,
        "median_dbfs": -120.0,
    }


def test_native_media_click_overlapping_dtmf_is_suppressed(monkeypatch):
    engine = _engine()
    samples = np.full(80000, 1200, dtype=np.int16)
    monkeypatch.setattr(media_module, "detect_unexpected_silence", lambda samples, sr: [])
    monkeypatch.setattr(media_module, "detect_click_pop_robust", lambda samples, sr: [{
        "time_seconds": 1.009,
        "confidence": 0.95,
        "jump": 12000,
        "energy_rise_db": 14.0,
        "highband_energy_ratio": 0.25,
    }])

    events = engine._active_media_audio_events(
        [_pcm_signal(samples)],
        _packet_result(),
        _pcm_result(dtmf_events=[{
            "digit": "6",
            "start_seconds": 1.0,
            "end_seconds": 1.14,
            "confidence": 0.99,
        }]),
        [_track(samples.copy())],
        _correlations(),
    )

    assert len(events) == 1
    decision = events[0]["details"]["candidate_decision"]
    assert decision["status"] == "SUPPRESSED"
    assert decision["reason_code"] == "NEGCTRL_DTMF_TRANSIENT"


def test_native_media_silence_with_active_correlated_rtp_is_promoted(monkeypatch):
    engine = _engine()
    pcm = np.full(80000, 1200, dtype=np.int16)
    rtp = np.full(80000, 2400, dtype=np.int16)
    monkeypatch.setattr(media_module, "detect_unexpected_silence", lambda samples, sr: [_silence_candidate()])
    monkeypatch.setattr(media_module, "detect_click_pop_robust", lambda samples, sr: [])

    events = engine._active_media_audio_events(
        [_pcm_signal(pcm)], _packet_result(), _pcm_result(), [_track(rtp)], _correlations(0.95)
    )

    assert len(events) == 1
    decision = events[0]["details"]["candidate_decision"]
    assert decision["status"] == "PROMOTED"
    assert decision["reason_code"] == "CROSS_LAYER_SILENCE_MISMATCH_CONFIRMED"
    assert decision["positive_evidence"]["counterpart_window"]["event_rms_dbfs"] > -42.0


def test_native_media_silence_with_matching_rtp_silence_is_suppressed(monkeypatch):
    engine = _engine()
    pcm = np.full(80000, 1200, dtype=np.int16)
    rtp = np.full(80000, 2400, dtype=np.int16)
    rtp[2 * 8000:int(2.5 * 8000)] = 0
    monkeypatch.setattr(media_module, "detect_unexpected_silence", lambda samples, sr: [_silence_candidate()])
    monkeypatch.setattr(media_module, "detect_click_pop_robust", lambda samples, sr: [])

    events = engine._active_media_audio_events(
        [_pcm_signal(pcm)], _packet_result(), _pcm_result(), [_track(rtp)], _correlations(0.95)
    )

    decision = events[0]["details"]["candidate_decision"]
    assert decision["status"] == "SUPPRESSED"
    assert decision["reason_code"] == "NEGCTRL_MATCHED_RTP_SOURCE_SILENCE"


def test_correlation_lag_aligns_rtp_source_window_before_silence_decision(monkeypatch):
    """Field-calibrated contract: positive PCM lag means compare RTP at PCM-time minus lag.

    The 44 ms tail is intentionally active at the unshifted PCM window. Without lag
    alignment the counterpart would look active and be falsely promoted. With the
    correlation-aligned source window the whole RTP counterpart is quiet and must be
    suppressed.
    """
    engine = _engine()
    pcm = np.full(80000, 1200, dtype=np.int16)
    rtp = np.full(80000, 2400, dtype=np.int16)
    lag_ms = 44.0
    aligned_start = 2.0 - lag_ms / 1000.0
    aligned_end = 2.5 - lag_ms / 1000.0
    rtp[int(round(aligned_start * 8000)):int(round(aligned_end * 8000))] = 0
    monkeypatch.setattr(media_module, "detect_unexpected_silence", lambda samples, sr: [_silence_candidate()])
    monkeypatch.setattr(media_module, "detect_click_pop_robust", lambda samples, sr: [])

    events = engine._active_media_audio_events(
        [_pcm_signal(pcm)],
        _packet_result(),
        _pcm_result(),
        [_track(rtp)],
        _correlations(0.95, lag_ms=lag_ms),
    )

    decision = events[0]["details"]["candidate_decision"]
    evidence = decision["positive_evidence"]
    assert decision["status"] == "SUPPRESSED"
    assert decision["reason_code"] == "NEGCTRL_MATCHED_RTP_SOURCE_SILENCE"
    assert evidence["correlation_lag_ms"] == lag_ms
    assert abs(evidence["counterpart_aligned_start_time"] - 101.956) < 1e-6
    assert abs(evidence["counterpart_aligned_end_time"] - 102.456) < 1e-6
    assert evidence["counterpart_window"]["event_rms_dbfs"] <= -52.0


def test_medium_correlation_cannot_drive_silence_decision(monkeypatch):
    engine = _engine()
    pcm = np.full(80000, 1200, dtype=np.int16)
    rtp = np.full(80000, 2400, dtype=np.int16)
    monkeypatch.setattr(media_module, "detect_unexpected_silence", lambda samples, sr: [_silence_candidate()])
    monkeypatch.setattr(media_module, "detect_click_pop_robust", lambda samples, sr: [])

    events = engine._active_media_audio_events(
        [_pcm_signal(pcm)], _packet_result(), _pcm_result(), [_track(rtp)], _correlations(0.70)
    )

    decision = events[0]["details"]["candidate_decision"]
    assert decision["status"] == "INCONCLUSIVE"
    assert decision["reason_code"] == "SILENCE_COUNTERPART_RTP_UNAVAILABLE_OR_WEAK"


def test_rtp_track_with_synthetic_gap_cannot_drive_silence_decision(monkeypatch):
    engine = _engine()
    pcm = np.full(80000, 1200, dtype=np.int16)
    rtp = np.full(80000, 2400, dtype=np.int16)
    monkeypatch.setattr(media_module, "detect_unexpected_silence", lambda samples, sr: [_silence_candidate()])
    monkeypatch.setattr(media_module, "detect_click_pop_robust", lambda samples, sr: [])

    events = engine._active_media_audio_events(
        [_pcm_signal(pcm)],
        _packet_result(),
        _pcm_result(),
        [_track(rtp, inserted_loss_samples=160)],
        _correlations(0.95),
    )

    decision = events[0]["details"]["candidate_decision"]
    evidence = decision["positive_evidence"]
    assert decision["status"] == "INCONCLUSIVE"
    assert decision["reason_code"] == "RTP_COUNTERPART_CONTAINS_SYNTHETIC_GAPS"
    assert evidence["counterpart_inserted_loss_samples"] == 160


def test_candidate_clip_writer_emits_only_promoted_candidates(tmp_path):
    engine = _engine()
    samples = np.full(80000, 1200, dtype=np.int16)
    pcm = _pcm_signal(samples)
    promoted = {
        "type": "CLICK_POP",
        "time": 105.0,
        "start_time": 105.0,
        "end_time": 105.0,
        "scope": {"pcm_tap": "pcm_tx", "pcm_session_index": 0},
        "details": {"candidate_decision": {
            "candidate_id": "cand-promoted-12345678",
            "status": "PROMOTED",
            "reason_code": "CLICK_POP_NEGATIVE_CONTROLS_CLEARED",
            "positive_evidence": {"correlation_lag_ms": 44.0},
        }},
    }
    suppressed = {
        "type": "UNEXPECTED_SILENCE",
        "time": 106.0,
        "start_time": 106.0,
        "end_time": 106.5,
        "scope": {"pcm_tap": "pcm_tx", "pcm_session_index": 0},
        "details": {"candidate_decision": {
            "candidate_id": "cand-suppressed-12345678",
            "status": "SUPPRESSED",
            "reason_code": "NEGCTRL_MATCHED_RTP_SOURCE_SILENCE",
        }},
    }

    result = engine._write_candidate_decision_clips([promoted, suppressed], [pcm], tmp_path)

    assert len(result["artifacts"]) == 1
    artifact = result["artifacts"][0]
    assert artifact["metadata"]["candidate_decision_status"] == "PROMOTED"
    assert artifact["metadata"]["event_type"] == "CLICK_POP"
    assert artifact["metadata"]["correlation_lag_ms"] == 44.0
    assert (tmp_path / artifact["filename"]).exists()
