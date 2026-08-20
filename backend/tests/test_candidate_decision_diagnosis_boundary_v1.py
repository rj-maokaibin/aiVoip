from app.analyzers.media.candidate_artifacts import sanitize_gated_media_pcm
from app.diagnosis.reasoner import DeterministicDiagnosisReasoner


def test_rejected_raw_pcm_candidates_do_not_reappear_as_diagnosis_hypotheses():
    result = {
        "packet": {"anomalies": [], "calls": [], "registrations": [], "rtp_streams": []},
        "correlations": [],
        "cross_layer_events": [],
        "pcm": {
            "streams": [{
                "tap": {"name": "pcm_rx"},
                "sessions": [{
                    "session_index": 0,
                    "hum": {},
                    "spectral": {},
                    "click_pop_events": [{"time_seconds": 4.332755, "confidence": 0.94}],
                    "silence_events": [{"start_seconds": 20.0, "end_seconds": 21.0, "duration_ms": 1000.0}],
                }],
            }],
        },
    }

    sanitize_gated_media_pcm(result)
    hypotheses = []
    known = []
    unknown = []
    excluded = []
    plan = []
    DeterministicDiagnosisReasoner()._reason_from_result(
        result,
        "media-run",
        hypotheses,
        known,
        unknown,
        excluded,
        plan,
        set(),
    )

    codes = {h.code for h in hypotheses}
    assert "PCM_CLICK_POP" not in codes
    assert "PCM_UNEXPECTED_SILENCE" not in codes
    session = result["pcm"]["streams"][0]["sessions"][0]
    assert session["click_pop_events"] == []
    assert session["silence_events"] == []
    assert len(session["click_pop_candidates"]) == 1
    assert len(session["silence_candidates"]) == 1
