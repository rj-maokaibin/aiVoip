from app.reports.diagnostic_contract import build_diagnostic_contract_snapshot
from app.reports.diagnostic_unresolved_pcm import append_unresolved_pcm_candidates


def test_media_unavailable_keeps_pcm_candidates_as_inconclusive_not_findings():
    pcm = {
        "summary": {"candidate_decision": {"status": "INCONCLUSIVE", "reason_code": "MEDIA_ANALYZER_UNAVAILABLE"}},
        "streams": [{
            "tap": {"name": "pcm_rx", "direction": "RX"},
            "sessions": [{
                "session_index": 0,
                "start_time": 100.0,
                "click_pop_candidates": [{"time_seconds": 3.0, "confidence": 0.92}],
                "silence_candidates": [{"start_seconds": 5.0, "end_seconds": 6.0, "duration_ms": 1000.0}],
            }],
        }],
    }
    states = {
        "packet_intelligence": {"status": "UNAVAILABLE"},
        "pcm_intelligence": {"status": "SUCCESS", "analyzer_version": "2", "config_version": "p1"},
        "media_intelligence": {"status": "UNAVAILABLE"},
    }
    results = {"packet_intelligence": None, "pcm_intelligence": pcm, "media_intelligence": None}
    snapshot = build_diagnostic_contract_snapshot(results=results, analyzer_states=states)
    append_unresolved_pcm_candidates(snapshot, results=results, analyzer_states=states)

    decisions = snapshot["candidate_decisions"]
    assert len(decisions) == 2
    assert {x["status"] for x in decisions} == {"INCONCLUSIVE"}
    assert {x["reason_code"] for x in decisions} == {"MEDIA_ANALYZER_UNAVAILABLE"}
    assert {x["event_type"] for x in decisions} == {"CLICK_POP", "UNEXPECTED_SILENCE"}
    assert snapshot["summary"]["decision_status_counts"] == {"INCONCLUSIVE": 2}


def test_unresolved_pcm_adapter_is_noop_when_media_gate_exists():
    pcm = {
        "streams": [{
            "tap": {"name": "pcm_rx", "direction": "RX"},
            "sessions": [{"session_index": 0, "start_time": 100.0, "click_pop_candidates": [{"time_seconds": 3.0}]}],
        }],
    }
    media = {"summary": {}, "candidate_decisions": [], "cross_layer_events": [], "periodic_interference_paths": []}
    states = {"packet_intelligence": {}, "pcm_intelligence": {}, "media_intelligence": {}}
    results = {"packet_intelligence": None, "pcm_intelligence": pcm, "media_intelligence": media}
    snapshot = build_diagnostic_contract_snapshot(results=results, analyzer_states=states)
    before = dict(snapshot["summary"])
    append_unresolved_pcm_candidates(snapshot, results=results, analyzer_states=states)
    assert snapshot["summary"] == before
    assert snapshot["candidate_decisions"] == []
