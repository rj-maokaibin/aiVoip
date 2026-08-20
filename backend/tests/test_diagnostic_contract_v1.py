from copy import deepcopy

from app.contracts.diagnostic import (
    CANDIDATE_DECISION_SCHEMA_VERSION,
    DIAGNOSTIC_EVENT_SCHEMA_VERSION,
    FINDING_DIAGNOSTIC_LINK_VERSION,
    CandidateDecisionStatus,
    build_candidate_decision,
    build_diagnostic_event,
    build_finding_diagnostic_link,
    validate_finding_diagnostic_link,
)
from app.reports.diagnostic_contract import (
    DIAGNOSTIC_CONTRACT_SNAPSHOT_VERSION,
    attach_finding_diagnostic_links,
    build_diagnostic_contract_snapshot,
    validate_diagnostic_contract_snapshot,
)
from app.services.evidence_boundary import apply_first_observable_boundaries


def _states():
    return {
        "packet_intelligence": {"run_id": "packet-run", "analyzer_version": "2.0", "config_version": "p1", "config_checksum": "a" * 64},
        "pcm_intelligence": {"run_id": "pcm-run", "analyzer_version": "2.0", "config_version": "p1", "config_checksum": "b" * 64},
        "media_intelligence": {"run_id": "media-run", "analyzer_version": "2.0", "config_version": "p1", "config_checksum": "c" * 64},
    }


def _legacy_candidate(candidate_id: str, status: str, when: float, reason: str):
    return {
        "schema_version": "candidate-decision-v1",
        "candidate_id": candidate_id,
        "candidate_type": "CLICK_POP",
        "candidate_time": when,
        "status": status,
        "reason_code": reason,
        "negative_controls": [{"type": reason}] if status == "REJECTED_NEGATIVE_CONTROL" else [],
        "source_event": {
            "type": "CLICK_POP",
            "time": when,
            "severity": "MEDIUM",
            "evidence_level": "L3",
            "scope": {"call_id": "call-1", "pcm_tap": "pcm_rx", "pcm_session_index": 0},
            "details": {"confidence": 0.95},
        },
    }


def test_canonical_event_and_decision_ids_are_stable():
    kwargs = dict(
        event_type="HIGH_DELTA",
        analyzer_id="packet_intelligence",
        analyzer_version="2.0",
        profile_version="p1",
        scope={"call_id": "call-1", "rtp_stream_id": "rtp-1", "direction": "DUT_TO_PBX"},
        time_range={"start": 100.0, "end": 100.0, "representative": 100.0},
        measurements={"delta_ms": 175.0, "sequence_continuous": True},
        source_ref={"source": "packet.anomalies", "index": 3},
    )
    event_a = build_diagnostic_event(**kwargs)
    event_b = build_diagnostic_event(**kwargs)
    assert event_a["schema_version"] == DIAGNOSTIC_EVENT_SCHEMA_VERSION
    assert event_a["event_id"] == event_b["event_id"]

    decision_a = build_candidate_decision(
        event_a,
        status=CandidateDecisionStatus.ACCEPT,
        reason_code="DETERMINISTIC_ANALYZER_EVENT_ACCEPTED",
        rule_version="rule-v1",
    )
    decision_b = build_candidate_decision(
        event_b,
        status="ACCEPT",
        reason_code="DETERMINISTIC_ANALYZER_EVENT_ACCEPTED",
        rule_version="rule-v1",
    )
    assert decision_a["schema_version"] == CANDIDATE_DECISION_SCHEMA_VERSION
    assert decision_a["decision_id"] == decision_b["decision_id"]
    link = build_finding_diagnostic_link(events=[event_a], decisions=[decision_a])
    assert link["schema_version"] == FINDING_DIAGNOSTIC_LINK_VERSION
    assert link["accepted_event_ids"] == [event_a["event_id"]]
    validate_finding_diagnostic_link(link)


def test_legacy_candidate_decisions_map_losslessly_to_canonical_dispositions():
    media = {
        "summary": {},
        "candidate_decisions": [
            _legacy_candidate("accepted", "PROMOTED", 10.0, "ACTIVE_MEDIA_MULTI_FEATURE_CLICK"),
            _legacy_candidate("suppressed", "REJECTED_NEGATIVE_CONTROL", 11.0, "DTMF_OVERLAP"),
            _legacy_candidate("unknown", "INCONCLUSIVE", 12.0, "COUNTERPART_ACTIVITY_NOT_PROVEN"),
        ],
        "cross_layer_events": [],
        "periodic_interference_paths": [],
    }
    snapshot = build_diagnostic_contract_snapshot(
        results={"packet_intelligence": None, "pcm_intelligence": None, "media_intelligence": media},
        analyzer_states=_states(),
    )
    assert snapshot["schema_version"] == DIAGNOSTIC_CONTRACT_SNAPSHOT_VERSION
    assert snapshot["summary"]["decision_status_counts"] == {
        "ACCEPT": 1,
        "INCONCLUSIVE": 1,
        "SUPPRESS": 1,
    }
    by_candidate = {x.get("legacy_candidate_id"): x for x in snapshot["candidate_decisions"]}
    assert by_candidate["accepted"]["status"] == "ACCEPT"
    assert by_candidate["accepted"]["legacy_status"] == "PROMOTED"
    assert by_candidate["suppressed"]["status"] == "SUPPRESS"
    assert by_candidate["unknown"]["status"] == "INCONCLUSIVE"


def test_suppressed_or_inconclusive_candidate_can_never_justify_finding():
    media = {
        "summary": {},
        "candidate_decisions": [
            _legacy_candidate("accepted", "PROMOTED", 10.0, "ACTIVE_MEDIA_MULTI_FEATURE_CLICK"),
            _legacy_candidate("suppressed", "REJECTED_NEGATIVE_CONTROL", 11.0, "DTMF_OVERLAP"),
            _legacy_candidate("unknown", "INCONCLUSIVE", 12.0, "COUNTERPART_ACTIVITY_NOT_PROVEN"),
        ],
        "cross_layer_events": [],
        "periodic_interference_paths": [],
    }
    snapshot = build_diagnostic_contract_snapshot(
        results={"packet_intelligence": None, "pcm_intelligence": None, "media_intelligence": media},
        analyzer_states=_states(),
    )
    finding = {
        "stable_key": "f-click",
        "type": "CLICK_POP",
        "severity": "MEDIUM",
        "evidence_level": "L3",
        "scope": {"call_id": "call-1", "pcm_tap": "pcm_rx", "pcm_session_index": 0},
        "time_range": {"start": 10.0, "end": 10.0, "representative": 10.0},
        "metrics": {"candidate_decision": {"candidate_id": "accepted"}},
        "evidence_refs": [],
    }
    attach_finding_diagnostic_links(findings=[finding], snapshot=snapshot)
    validate_diagnostic_contract_snapshot(snapshot, findings=[finding])

    linked_decisions = {
        x["decision_id"]: x for x in snapshot["candidate_decisions"]
        if x["decision_id"] in finding["diagnostic"]["decision_ids"]
    }
    assert linked_decisions
    assert {x["status"] for x in linked_decisions.values()} == {"ACCEPT"}
    suppressed_ids = {
        x["event_id"] for x in snapshot["candidate_decisions"]
        if x["status"] in {"SUPPRESS", "INCONCLUSIVE"}
    }
    assert suppressed_ids.isdisjoint(set(finding["diagnostic"]["event_ids"]))


def test_multiple_accepted_events_gain_explicit_merge_decision():
    packet = {
        "rtp_streams": [{"stream_id": "rtp-1", "ssrc": "0x1", "lost_packets": 0, "max_delta_ms": 175.0, "ptime_ms": 20.0}],
        "anomalies": [
            {"type": "HIGH_DELTA", "time": 10.0, "severity": "MEDIUM", "evidence": {"stream_id": "rtp-1", "call_id": "call-1", "delta_ms": 146.0}},
            {"type": "HIGH_DELTA", "time": 12.0, "severity": "MEDIUM", "evidence": {"stream_id": "rtp-1", "call_id": "call-1", "delta_ms": 175.0}},
        ],
    }
    snapshot = build_diagnostic_contract_snapshot(
        results={"packet_intelligence": packet, "pcm_intelligence": None, "media_intelligence": None},
        analyzer_states=_states(),
    )
    finding = {
        "stable_key": "f-delta",
        "type": "HIGH_DELTA",
        "severity": "MEDIUM",
        "evidence_level": "L2",
        "scope": {"call_id": "call-1", "rtp_stream_id": "rtp-1"},
        "time_range": {"start": 10.0, "end": 12.0, "representative": 10.0},
        "metrics": {},
        "evidence_refs": [],
    }
    attach_finding_diagnostic_links(findings=[finding], snapshot=snapshot)
    validate_diagnostic_contract_snapshot(snapshot, findings=[finding])
    assert len(finding["diagnostic"]["event_ids"]) == 2
    assert len(finding["diagnostic"]["merged_event_ids"]) == 1
    merge_rows = [
        x for x in snapshot["candidate_decisions"]
        if x["status"] == CandidateDecisionStatus.MERGE.value
    ]
    assert len(merge_rows) == 1
    assert merge_rows[0]["merge_target_event_id"] in finding["diagnostic"]["event_ids"]


def test_evidence_boundary_promotes_snapshot_to_report_and_persists_compact_refs():
    media = {
        "summary": {},
        "candidate_decisions": [
            _legacy_candidate("accepted", "PROMOTED", 10.0, "ACTIVE_MEDIA_MULTI_FEATURE_CLICK"),
        ],
        "cross_layer_events": [],
        "periodic_interference_paths": [],
    }
    snapshot = build_diagnostic_contract_snapshot(
        results={"packet_intelligence": None, "pcm_intelligence": None, "media_intelligence": media},
        analyzer_states=_states(),
    )
    finding = {
        "stable_key": "f-click",
        "type": "CLICK_POP",
        "severity": "MEDIUM",
        "evidence_level": "L3",
        "scope": {"call_id": "call-1", "pcm_tap": "pcm_rx", "pcm_session_index": 0},
        "time_range": {"start": 10.0, "end": 10.0, "representative": 10.0},
        "metrics": {"candidate_decision": {"candidate_id": "accepted"}},
        "correlation": {},
        "evidence_refs": [],
    }
    payload = {
        "media_summary": {"__diagnostic_contract_snapshot": deepcopy(snapshot)},
        "pcm_summary": {"summary": {}},
        "findings": [finding],
    }
    apply_first_observable_boundaries(payload)
    assert "__diagnostic_contract_snapshot" not in payload["media_summary"]
    assert payload["diagnostic_contract"]["schema_version"] == DIAGNOSTIC_CONTRACT_SNAPSHOT_VERSION
    assert payload["findings"][0]["diagnostic"]["accepted_event_ids"]
    compact = payload["findings"][0]["correlation"]["diagnostic_contract"]
    assert compact["event_ids"] == payload["findings"][0]["diagnostic"]["event_ids"]
    assert compact["decision_ids"] == payload["findings"][0]["diagnostic"]["decision_ids"]
