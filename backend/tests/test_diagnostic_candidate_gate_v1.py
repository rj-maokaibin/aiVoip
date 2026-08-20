from app.analyzers.candidate_gate import (
    COUNTERPART_RTP_ACTIVE,
    COUNTERPART_RTP_ACTIVITY_UNAVAILABLE,
    COUNTERPART_RTP_LOW_ENERGY,
    COUNTERPART_RTP_SILENCE,
    DTMF_TRANSIENT_OVERLAP,
    OUTSIDE_ACTIVE_MEDIA_WINDOW,
    RTP_MAPPING_MISSING,
    CandidateDecision,
    build_diagnostic_candidates,
)
from app.reports.finding_composer import compose_findings


def _pcm(*, dtmf=None, raw_click=True, raw_silence=True):
    return {
        "streams": [{
            "tap": {"name": "pcm_tx", "direction": "TX"},
            "sessions": [{
                "session_index": 1,
                "start_time": 10.0,
                "end_time": 13.0,
                "hum": {"level": "LOW"},
                "gap_events": [{"time": 10.2, "delta_ms": 80.0}],
                "silence_events": ([{"start_seconds": 0.5, "end_seconds": 1.0, "duration_ms": 500.0}] if raw_silence else []),
                "click_pop_events": ([{"time_seconds": 0.51, "jump": 9000.0, "confidence": 0.9}] if raw_click else []),
                "dtmf_events": dtmf or [],
                "dtmf_quality_events": [],
            }],
        }]
    }


def _event(ftype, when=10.5, *, duration_ms=500.0):
    details = {"absolute_time": when, "confidence": 0.9}
    if ftype == "UNEXPECTED_SILENCE":
        details = {"absolute_start_time": when, "duration_ms": duration_ms, "threshold_dbfs": -50.0}
    return {
        "type": ftype,
        "time": when,
        "severity": "MEDIUM",
        "evidence_level": "L3",
        "scope": {"call_id": "call-1", "pcm_tap": "pcm_tx", "pcm_session_index": 1},
        "details": details,
    }


def _activity_profile(levels):
    return {
        "stream_id": "rtp-down",
        "start_time": 10.0,
        "end_time": 13.0,
        "source_artifact": "rtp-down-waveform.json",
        "waveform": {
            "sample_rate": 4,
            "bin_size_samples": 1,
            "duration_seconds": len(levels) / 4,
            "bins": [{"t": i / 4, "rms_dbfs": float(level)} for i, level in enumerate(levels)],
        },
    }


def _media(events, *, correlation=0.8, rtp_silence=None, rtp_levels=None, include_activity=True, call_window=(10.0, 13.0)):
    if rtp_levels is None:
        rtp_levels = [-20.0] * 12
    result = {
        "active_media_audio_events": events,
        "cross_layer_events": list(events),
        "correlations": [{
            "type": "PCM_RTP_CORRELATION",
            "details": {
                "pcm_tap": "pcm_tx",
                "pcm_session_index": 1,
                "rtp_stream_id": "rtp-down",
                "correlation": {"absolute_correlation": correlation, "quality": "HIGH" if correlation >= 0.8 else "LOW"},
            },
        }],
        "rtp_audio_tracks": [{
            "stream_id": "rtp-down",
            "start_time": 10.0,
            "end_time": 13.0,
            "silence_events": rtp_silence or [],
        }],
        "periodic_interference_paths": [],
    }
    if call_window is not None:
        result["packet"] = {"calls": [{"call_id": "call-1", "media_start_time": call_window[0], "media_end_time": call_window[1]}]}
    if include_activity:
        result["rtp_activity_profiles"] = [_activity_profile(rtp_levels)]
    return result


def test_raw_pcm_click_and_silence_are_detector_only_and_cannot_bypass_candidate_gate():
    findings = compose_findings(pcm=_pcm(), media=None, source_run_ids={"pcm_intelligence": "pcm-run"})
    types = [x["type"] for x in findings]
    assert "PCM_GAP" in types
    assert "CLICK_POP" not in types
    assert "UNEXPECTED_SILENCE" not in types


def test_raw_pre_call_candidates_are_audited_as_suppressed_not_silently_dropped():
    pcm = _pcm(dtmf=[{
        "digit": "6", "start_seconds": 0.50, "end_seconds": 0.64,
        "duration_ms": 140.0, "confidence": 0.95,
    }])
    media = _media([], call_window=(12.0, 13.0))
    candidates = build_diagnostic_candidates(pcm=pcm, media=media)
    assert len(candidates) == 2
    assert all(x["decision"] == CandidateDecision.SUPPRESS.value for x in candidates)
    assert all(OUTSIDE_ACTIVE_MEDIA_WINDOW in x["reason_codes"] for x in candidates)
    click = next(x for x in candidates if x["type"] == "CLICK_POP")
    assert DTMF_TRANSIENT_OVERLAP in click["reason_codes"]
    assert click["context"]["overlapping_dtmf"]["digit"] == "6"

    media["diagnostic_candidates"] = candidates
    findings = compose_findings(pcm=pcm, media=media, source_run_ids={"media_intelligence": "media-run"})
    assert "CLICK_POP" not in [x["type"] for x in findings]
    assert "UNEXPECTED_SILENCE" not in [x["type"] for x in findings]


def test_click_overlapping_dtmf_is_suppressed_and_never_becomes_finding():
    pcm = _pcm(dtmf=[{
        "digit": "6", "start_seconds": 0.50, "end_seconds": 0.64,
        "duration_ms": 140.0, "confidence": 0.95,
    }])
    media = _media([_event("CLICK_POP", 10.51)])
    candidates = build_diagnostic_candidates(pcm=pcm, media=media)
    assert len(candidates) == 1
    assert candidates[0]["decision"] == CandidateDecision.SUPPRESS.value
    assert DTMF_TRANSIENT_OVERLAP in candidates[0]["reason_codes"]
    assert candidates[0]["context"]["overlapping_dtmf"]["digit"] == "6"

    media["diagnostic_candidates"] = candidates
    findings = compose_findings(pcm=pcm, media=media, source_run_ids={"media_intelligence": "media-run"})
    assert "CLICK_POP" not in [x["type"] for x in findings]


def test_click_outside_dtmf_window_is_accepted_with_candidate_provenance():
    pcm = _pcm(dtmf=[{
        "digit": "6", "start_seconds": 0.10, "end_seconds": 0.24,
        "duration_ms": 140.0, "confidence": 0.95,
    }])
    media = _media([_event("CLICK_POP", 11.0)])
    candidates = build_diagnostic_candidates(pcm=pcm, media=media)
    assert candidates[0]["decision"] == CandidateDecision.ACCEPT.value
    media["diagnostic_candidates"] = candidates

    findings = compose_findings(pcm=pcm, media=media, source_run_ids={"media_intelligence": "media-run"})
    click = next(x for x in findings if x["type"] == "CLICK_POP")
    assert click["metrics"]["candidate_id"] == candidates[0]["candidate_id"]
    assert click["metrics"]["candidate_decision"] == "ACCEPT"


def test_silence_is_suppressed_when_correlated_rtp_window_is_low_energy():
    pcm = _pcm()
    media = _media([_event("UNEXPECTED_SILENCE", 10.5, duration_ms=500.0)], rtp_levels=[-82.0] * 12)
    candidates = build_diagnostic_candidates(pcm=pcm, media=media)
    candidate = next(x for x in candidates if x["type"] == "UNEXPECTED_SILENCE" and "ACTIVE_MEDIA_SCOPED" in x["reason_codes"])
    assert candidate["decision"] == CandidateDecision.SUPPRESS.value
    assert COUNTERPART_RTP_LOW_ENERGY in candidate["reason_codes"]
    assert candidate["context"]["counterpart_rtp_activity"]["status"] == "LOW_ENERGY"

    media["diagnostic_candidates"] = candidates
    findings = compose_findings(pcm=pcm, media=media, source_run_ids={"media_intelligence": "media-run"})
    assert "UNEXPECTED_SILENCE" not in [x["type"] for x in findings]


def test_silence_is_accepted_only_with_positive_correlated_rtp_activity_evidence():
    pcm = _pcm()
    media = _media([_event("UNEXPECTED_SILENCE", 10.5, duration_ms=500.0)], rtp_levels=[-20.0] * 12)
    candidates = build_diagnostic_candidates(pcm=pcm, media=media)
    candidate = next(x for x in candidates if x["type"] == "UNEXPECTED_SILENCE" and "ACTIVE_MEDIA_SCOPED" in x["reason_codes"])
    assert candidate["decision"] == CandidateDecision.ACCEPT.value
    assert COUNTERPART_RTP_ACTIVE in candidate["reason_codes"]
    assert candidate["context"]["counterpart_rtp_activity"]["active_fraction"] >= 0.2

    media["diagnostic_candidates"] = candidates
    findings = compose_findings(pcm=pcm, media=media, source_run_ids={"media_intelligence": "media-run"})
    silence = next(x for x in findings if x["type"] == "UNEXPECTED_SILENCE")
    assert silence["metrics"]["counterpart_rtp_stream_id"] == "rtp-down"
    assert silence["metrics"]["pcm_rtp_absolute_correlation"] == 0.8


def test_legacy_result_with_explicit_counterpart_silence_can_suppress_but_cannot_accept():
    pcm = _pcm()
    media = _media(
        [_event("UNEXPECTED_SILENCE", 10.5, duration_ms=500.0)],
        rtp_silence=[{"start_seconds": 0.45, "end_seconds": 1.10, "duration_ms": 650.0}],
        include_activity=False,
    )
    candidate = next(x for x in build_diagnostic_candidates(pcm=pcm, media=media) if x["type"] == "UNEXPECTED_SILENCE" and "ACTIVE_MEDIA_SCOPED" in x["reason_codes"])
    assert candidate["decision"] == CandidateDecision.SUPPRESS.value
    assert COUNTERPART_RTP_SILENCE in candidate["reason_codes"]

    media = _media([_event("UNEXPECTED_SILENCE", 10.5)], include_activity=False)
    candidate = next(x for x in build_diagnostic_candidates(pcm=pcm, media=media) if x["type"] == "UNEXPECTED_SILENCE" and "ACTIVE_MEDIA_SCOPED" in x["reason_codes"])
    assert candidate["decision"] == CandidateDecision.INCONCLUSIVE.value
    assert COUNTERPART_RTP_ACTIVITY_UNAVAILABLE in candidate["reason_codes"]


def test_silence_without_cross_layer_rtp_mapping_stays_inconclusive():
    pcm = _pcm()
    media = _media([_event("UNEXPECTED_SILENCE", 10.5)])
    media["correlations"] = []
    candidates = build_diagnostic_candidates(pcm=pcm, media=media)
    candidate = next(x for x in candidates if x["type"] == "UNEXPECTED_SILENCE" and "ACTIVE_MEDIA_SCOPED" in x["reason_codes"])
    assert candidate["decision"] == CandidateDecision.INCONCLUSIVE.value
    assert RTP_MAPPING_MISSING in candidate["reason_codes"]

    media["diagnostic_candidates"] = candidates
    findings = compose_findings(pcm=pcm, media=media, source_run_ids={"media_intelligence": "media-run"})
    assert "UNEXPECTED_SILENCE" not in [x["type"] for x in findings]
