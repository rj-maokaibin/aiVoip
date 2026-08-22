from copy import deepcopy

from app.reports.diagnostic_contract import build_diagnostic_contract_snapshot
from app.reports.report_grounding import build_claim_manifest
from app.services.evidence_boundary import apply_first_observable_boundaries


SNAPSHOT_KEY = "__diagnostic_contract_snapshot"


def test_claim_manifest_sees_canonical_event_and_decision_refs_without_losing_legacy_provenance():
    packet = {
        "rtp_streams": [{"stream_id": "rtp-1", "ssrc": "0x1", "lost_packets": 0, "ptime_ms": 20.0}],
        "anomalies": [{
            "type": "HIGH_DELTA",
            "time": 10.0,
            "severity": "MEDIUM",
            "evidence": {
                "call_id": "call-1",
                "stream_id": "rtp-1",
                "delta_ms": 146.0,
                "sequence_continuous": True,
            },
        }],
    }
    states = {
        "packet_intelligence": {"run_id": "packet-run", "status": "SUCCESS", "analyzer_version": "2"},
        "pcm_intelligence": {"status": "UNAVAILABLE"},
        "media_intelligence": {"status": "UNAVAILABLE"},
    }
    snapshot = build_diagnostic_contract_snapshot(
        results={"packet_intelligence": packet, "pcm_intelligence": None, "media_intelligence": None},
        analyzer_states=states,
    )
    states["packet_intelligence"][SNAPSHOT_KEY] = deepcopy(snapshot)
    finding = {
        "stable_key": "delta-stable",
        "type": "HIGH_DELTA",
        "severity": "MEDIUM",
        "evidence_level": "L2",
        "observation": "RTP 间隔异常增大。",
        "scope": {"call_id": "call-1", "rtp_stream_id": "rtp-1", "ssrc": "0x1"},
        "time_range": {"start": 10.0, "end": 10.0, "representative": 10.0},
        "metrics": {"max_delta_ms": 146.0, "stream_lost_packets": 0, "all_sequence_continuous": True},
        "event_refs": [{"source": "packet.anomalies", "index": 0}],
        "evidence_refs": [],
        "artifact_refs": [],
        "correlation": {},
    }
    payload = {
        "analyzers": states,
        "findings": [finding],
        "media_summary": None,
        "pcm_summary": {"available": False},
    }

    apply_first_observable_boundaries(payload)
    manifest = build_claim_manifest(payload)

    refs = manifest["claims"][0]["event_refs"]
    assert {"source": "packet.anomalies", "index": 0} in refs
    assert any(x.get("source") == "diagnostic.events" and x.get("event_id") for x in refs)
    assert any(
        x.get("source") == "diagnostic.decisions"
        and x.get("decision_id")
        and x.get("status") == "ACCEPT"
        for x in refs
    )
