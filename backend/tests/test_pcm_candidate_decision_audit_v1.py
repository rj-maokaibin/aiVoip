from app.reports.candidate_decision import pcm_candidate_decision_summary
from app.reports.finding_composer import build_normal_evidence


def _pcm_result() -> dict:
    return {
        "summary": {"total_packets": 100},
        "streams": [{
            "tap": {"name": "pcm_rx", "direction": "RX"},
            "sessions": [{
                "session_index": 0,
                "click_pop_events": [{
                    "time_seconds": 9.0,
                    "candidate_decision": {
                        "candidate_id": "cand-dtmf-click",
                        "candidate_type": "CLICK_POP",
                        "status": "SUPPRESSED",
                        "reason_code": "NEGCTRL_DTMF_TRANSIENT",
                        "scope": {"pcm_tap": "pcm_rx", "pcm_session_index": 0},
                    },
                }],
                "silence_events": [{
                    "start_seconds": 10.0,
                    "end_seconds": 10.5,
                    "candidate_decision": {
                        "candidate_id": "cand-raw-silence",
                        "candidate_type": "UNEXPECTED_SILENCE",
                        "status": "INCONCLUSIVE",
                        "reason_code": "RAW_PCM_REQUIRES_CROSS_LAYER_COUNTERPART",
                        "scope": {"pcm_tap": "pcm_rx", "pcm_session_index": 0},
                    },
                }],
            }],
        }],
    }


def test_pcm_candidate_decision_summary_preserves_suppression_reason():
    summary = pcm_candidate_decision_summary(_pcm_result())

    assert summary["candidate_count"] == 2
    assert summary["suppressed"] == 1
    assert summary["inconclusive"] == 1
    assert summary["by_reason"]["NEGCTRL_DTMF_TRANSIENT"] == 1
    assert summary["by_reason"]["RAW_PCM_REQUIRES_CROSS_LAYER_COUNTERPART"] == 1


def test_report_exclusion_evidence_explains_raw_pcm_candidate_filtering():
    normal = build_normal_evidence(None, _pcm_result(), None)
    by_type = {x["type"]: x for x in normal}

    assert "PCM_RAW_CANDIDATES_SUPPRESSED_BY_NEGATIVE_CONTROL" in by_type
    assert "PCM_RAW_CANDIDATES_INCONCLUSIVE" in by_type
    assert "NEGCTRL_DTMF_TRANSIENT" in by_type["PCM_RAW_CANDIDATES_SUPPRESSED_BY_NEGATIVE_CONTROL"]["details"]["by_reason"]
    assert "不拥有异常 Finding 权限" in by_type["PCM_RAW_CANDIDATES_SUPPRESSED_BY_NEGATIVE_CONTROL"]["text"]
