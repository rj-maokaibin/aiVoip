from copy import deepcopy

from app.reports.diagnostic_contract import build_diagnostic_contract_snapshot
from app.services.evidence_boundary import apply_first_observable_boundaries


SNAPSHOT_KEY = "__diagnostic_contract_snapshot"


def _states():
    return {
        "packet_intelligence": {
            "run_id": "packet-run",
            "status": "SUCCESS",
            "analyzer_version": "2.0",
            "config_version": "profile-v1",
            "config_checksum": "a" * 64,
        },
        "pcm_intelligence": {"status": "UNAVAILABLE"},
        "media_intelligence": {"status": "UNAVAILABLE"},
    }


def test_packet_only_snapshot_survives_transport_without_finding_fallback():
    packet = {
        "rtp_streams": [{
            "stream_id": "rtp-up",
            "src_ip": "192.168.150.4",
            "src_port": 10000,
            "dst_ip": "192.168.3.200",
            "dst_port": 11446,
            "ssrc": "0x737715a5",
            "lost_packets": 0,
            "loss_rate": 0.0,
            "max_delta_ms": 175.043,
            "ptime_ms": 20.0,
            "call_direction_role": "DUT_TO_PBX",
        }],
        "anomalies": [{
            "type": "HIGH_DELTA",
            "time": 120.0,
            "severity": "MEDIUM",
            "evidence": {
                "call_id": "call-1",
                "stream_id": "rtp-up",
                "ssrc": "0x737715a5",
                "delta_ms": 175.043,
                "sequence_continuous": True,
                "previous_sequence": 100,
                "current_sequence": 101,
            },
        }],
    }
    states = _states()
    snapshot = build_diagnostic_contract_snapshot(
        results={"packet_intelligence": packet, "pcm_intelligence": None, "media_intelligence": None},
        analyzer_states=states,
    )
    states["packet_intelligence"][SNAPSHOT_KEY] = deepcopy(snapshot)
    finding = {
        "stable_key": "delta",
        "type": "HIGH_DELTA",
        "severity": "MEDIUM",
        "evidence_level": "L2",
        "scope": {
            "call_id": "call-1",
            "rtp_stream_id": "rtp-up",
            "direction": "192.168.150.4:10000->192.168.3.200:11446",
            "ssrc": "0x737715a5",
        },
        "time_range": {"start": 120.0, "end": 120.0, "representative": 120.0},
        "metrics": {"stream_lost_packets": 0, "all_sequence_continuous": True},
        "correlation": {},
        "evidence_refs": [],
    }
    payload = {
        "analyzers": states,
        "media_summary": None,
        "pcm_summary": {"available": False},
        "findings": [finding],
    }

    apply_first_observable_boundaries(payload)

    assert SNAPSHOT_KEY not in payload["analyzers"]["packet_intelligence"]
    assert payload["diagnostic_contract"]["summary"]["finding_fallback_event_count"] == 0
    link = payload["findings"][0]["diagnostic"]
    assert len(link["accepted_event_ids"]) == 1
    event = next(x for x in payload["diagnostic_contract"]["events"] if x["event_id"] in link["event_ids"])
    assert event["analyzer"]["id"] == "packet_intelligence"
    assert event["event_type"] == "HIGH_DELTA"
    assert event["measurements"]["sequence_continuous"] is True


def test_role_direction_is_preserved_but_not_used_as_endpoint_identity():
    packet = {
        "rtp_streams": [{
            "stream_id": "rtp-up",
            "ssrc": "0x1",
            "lost_packets": 0,
            "call_direction_role": "DUT_TO_PBX",
        }],
        "anomalies": [{
            "type": "HIGH_DELTA",
            "time": 10.0,
            "severity": "MEDIUM",
            "evidence": {"call_id": "call-1", "stream_id": "rtp-up", "delta_ms": 146.0},
        }],
    }
    snapshot = build_diagnostic_contract_snapshot(
        results={"packet_intelligence": packet, "pcm_intelligence": None, "media_intelligence": None},
        analyzer_states=_states(),
    )
    event = snapshot["events"][0]
    assert event["scope"]["direction"] is None
    assert event["scope"]["extensions"]["direction_role"] == "DUT_TO_PBX"
