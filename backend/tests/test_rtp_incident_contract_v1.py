from app.analyzers.cross_layer import (
    build_cross_layer_observation,
    derive_first_observable_layer,
    silence_candidate_observation,
)
from app.analyzers.packet.incidents import build_rtp_incidents, enrich_packet_anomalies
from app.reports.finding_composer import compose_findings


def _call():
    return {
        "call_id": "call-601",
        "start_time": 100.0,
        "media_start_time": 103.0,
        "media_end_time": 160.0,
        "rtp_stream_ids": ["rtp-up"],
        "media_direction_health": {
            "endpoint_a": {"ip": "192.168.150.4", "port": 10000},
            "endpoint_b": {"ip": "192.168.3.200", "port": 11446},
            "status": "BIDIRECTIONAL",
        },
    }


def _stream(*, previous_sequence=100, current_sequence=101, delta_ms=146.083, lost_packets=0):
    return {
        "stream_id": "rtp-up",
        "src_ip": "192.168.150.4",
        "src_port": 10000,
        "dst_ip": "192.168.3.200",
        "dst_port": 11446,
        "ssrc": 1234,
        "packet_count": 2423,
        "lost_packets": lost_packets,
        "loss_rate": 0.0 if lost_packets == 0 else 0.1,
        "p95_rfc3550_jitter_ms": 1.8,
        "max_delta_ms": delta_ms,
        "codec": "PCMU",
        "ptime_ms": 20.0,
        "events": [{
            "type": "HIGH_DELTA",
            "start_time": 140.135,
            "severity": "MEDIUM",
            "details": {
                "delta_ms": delta_ms,
                "expected_ptime_ms": 20.0,
                "excess_delay_ms": round(delta_ms - 20.0, 3),
                "previous_frame_number": 12891,
                "current_frame_number": 12892,
                "previous_timestamp": 139.989,
                "current_timestamp": 140.135,
                "previous_sequence": previous_sequence,
                "current_sequence": current_sequence,
            },
        }],
    }


def _packet_with_incident(stream):
    calls = [_call()]
    incidents = build_rtp_incidents(calls, [stream])
    anomalies = enrich_packet_anomalies([{
        "type": "HIGH_DELTA",
        "severity": "MEDIUM",
        "time": 140.135,
        "evidence": {"stream_id": "rtp-up", **stream["events"][0]["details"]},
    }], incidents)
    return {
        "summary": {"call_count": 1, "rtp_stream_count": 1},
        "calls": calls,
        "rtp_streams": [stream],
        "rtp_incidents": incidents,
        "rtp_incident_summary": {
            "count": 1,
            "by_type": {"HIGH_DELTA": 1},
            "cadence_stall_without_sequence_gap_count": sum(
                1 for x in incidents if x["semantic_code"] == "RTP_CADENCE_STALL_WITHOUT_SEQUENCE_GAP"
            ),
        },
        "anomalies": anomalies,
    }


def test_high_delta_contiguous_sequence_is_cadence_stall_not_packet_loss():
    incidents = build_rtp_incidents([_call()], [_stream()])
    assert len(incidents) == 1
    incident = incidents[0]
    assert incident["semantic_code"] == "RTP_CADENCE_STALL_WITHOUT_SEQUENCE_GAP"
    assert incident["sequence_boundary"]["sequence_contiguous"] is True
    assert incident["sequence_boundary"]["packet_loss_at_boundary"] == 0
    assert incident["measurements"]["stream_lost_packets"] == 0
    assert incident["call_id"] == "call-601"
    assert incident["call_relative_time_seconds"] == 37.135
    assert incident["media_role"] == "OFFERER_TO_ANSWERER"
    assert [x["frame_number"] for x in incident["packet_refs"]] == [12891, 12892]


def test_high_delta_sequence_gap_explicitly_reports_boundary_loss():
    incident = build_rtp_incidents([_call()], [_stream(previous_sequence=100, current_sequence=104, lost_packets=3)])[0]
    assert incident["semantic_code"] == "RTP_CADENCE_STALL_WITH_SEQUENCE_GAP"
    assert incident["sequence_boundary"]["sequence_contiguous"] is False
    assert incident["sequence_boundary"]["sequence_step"] == 4
    assert incident["sequence_boundary"]["packet_loss_at_boundary"] == 3


def test_enriched_packet_anomaly_preserves_incident_call_direction_and_frames():
    stream = _stream()
    packet = _packet_with_incident(stream)
    evidence = packet["anomalies"][0]["evidence"]
    assert evidence["incident_id"].startswith("rtpi-")
    assert evidence["call_id"] == "call-601"
    assert evidence["direction"]["text"] == "192.168.150.4:10000->192.168.3.200:11446"
    assert evidence["sequence_boundary"]["sequence_contiguous"] is True
    assert evidence["packet_refs"][0]["frame_number"] == 12891


def test_high_delta_finding_uses_human_semantics_and_preserves_incident_metrics():
    packet = _packet_with_incident(_stream())
    findings = compose_findings(packet=packet, source_run_ids={"packet_intelligence": "packet-run"})
    finding = next(x for x in findings if x["type"] == "HIGH_DELTA")
    assert finding["title"] == "RTP 发送/到达节奏短时停顿（High Delta）"
    assert "146.083" in finding["observation"]
    assert "20" in finding["observation"]
    assert "不等同于 RTP Packet Loss" in finding["interpretation"]
    assert finding["metrics"]["packet_loss_relation"] == "NO_SEQUENCE_GAP_AND_STREAM_LOSS_ZERO"
    assert finding["metrics"]["incident_count"] == 1
    assert finding["metrics"]["incidents"][0]["packet_refs"][1]["frame_number"] == 12892
    assert any(x.get("source") == "pcap.frame" for x in finding["event_refs"])


def test_multiple_high_delta_events_same_stream_are_one_finding_with_incident_list():
    stream = _stream()
    second = {
        "type": "HIGH_DELTA",
        "start_time": 140.479,
        "severity": "MEDIUM",
        "details": {
            "delta_ms": 175.043,
            "expected_ptime_ms": 20.0,
            "excess_delay_ms": 155.043,
            "previous_frame_number": 12910,
            "current_frame_number": 12911,
            "previous_timestamp": 140.304,
            "current_timestamp": 140.479,
            "previous_sequence": 120,
            "current_sequence": 121,
        },
    }
    stream["events"].append(second)
    calls = [_call()]
    incidents = build_rtp_incidents(calls, [stream])
    anomalies = enrich_packet_anomalies([
        {"type": e["type"], "severity": e["severity"], "time": e["start_time"], "evidence": {"stream_id": "rtp-up", **e["details"]}}
        for e in stream["events"]
    ], incidents)
    packet = {
        "calls": calls,
        "rtp_streams": [stream],
        "anomalies": anomalies,
        "rtp_incidents": incidents,
        "rtp_incident_summary": {"count": 2, "by_type": {"HIGH_DELTA": 2}, "cadence_stall_without_sequence_gap_count": 2},
    }
    findings = [x for x in compose_findings(packet=packet) if x["type"] == "HIGH_DELTA"]
    assert len(findings) == 1
    finding = findings[0]
    assert finding["occurrence_count"] == 2
    assert finding["metrics"]["incident_count"] == 2
    assert "146.083" in finding["observation"] and "175.043" in finding["observation"]


def test_cross_layer_boundary_requires_available_normal_upstream_control():
    observed = derive_first_observable_layer([
        {"layer": "CORRELATED_RTP_INPUT", "available": True, "abnormal": False},
        {"layer": "PCM_TX", "available": True, "abnormal": True},
    ])
    assert observed["status"] == "OBSERVED_BOUNDARY"
    assert observed["first_observable_layer"] == "PCM_TX"

    unknown = derive_first_observable_layer([
        {"layer": "CORRELATED_RTP_INPUT", "available": False, "abnormal": False},
        {"layer": "PCM_TX", "available": True, "abnormal": True},
    ])
    assert unknown["status"] == "UNKNOWN"
    assert unknown["reason"] == "UPSTREAM_EVIDENCE_MISSING"


def test_accepted_silence_candidate_has_cross_layer_boundary_at_pcm_not_physical_root():
    candidate = {
        "candidate_id": "cand-silence",
        "type": "UNEXPECTED_SILENCE",
        "decision": "ACCEPT",
        "reason_codes": ["ACTIVE_MEDIA_SCOPED", "COUNTERPART_RTP_ACTIVE"],
        "time_range": {"start": 10.5, "end": 11.0, "representative": 10.5},
        "scope": {"call_id": "call-1", "pcm_tap": "pcm_tx", "pcm_session_index": 1},
        "metrics": {"duration_ms": 500},
        "context": {
            "counterpart_rtp_stream_id": "rtp-down",
            "counterpart_rtp_activity": {"status": "ACTIVE", "active_fraction": 0.8},
        },
    }
    observation = silence_candidate_observation(candidate)
    assert observation is not None
    boundary = observation["first_observable_boundary"]
    assert boundary["status"] == "OBSERVED_BOUNDARY"
    assert boundary["first_observable_layer"] == "PCM_TX"
    assert observation["layers"][0]["layer"] == "CORRELATED_RTP_INPUT"
    assert "物理根因" in observation["root_cause_boundary"]


def test_cross_layer_builder_keeps_unknown_when_control_is_missing():
    row = build_cross_layer_observation(
        observation_type="TEST_PATH",
        call_id="c",
        time_range={"start": 1.0, "end": 2.0},
        layers=[
            {"layer": "CONTROL", "available": False, "abnormal": False},
            {"layer": "OBSERVED", "available": True, "abnormal": True},
        ],
    )
    assert row["first_observable_boundary"]["status"] == "UNKNOWN"
