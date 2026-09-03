from app.reports.v2.runtime_adapter import compose_v2_runtime_from_analyzers


def _selected_call():
    return {
        "call_id": "leg-a",
        "caller": "sip:601@pbx",
        "callee": "sip:101@pbx",
        "state": "ESTABLISHED",
        "start_time": 1.0,
        "end_time": 2.0,
        "media_start_time": 2.0,
        "media_end_time": 2.0,
        "rtp_stream_ids": ["caller-up", "caller-down"],
        "ladder": [
            {"frame_number": 1, "timestamp": 1.0, "method": "INVITE", "cseq_method": "INVITE", "src": "192.168.150.10:5060", "dst": "192.168.3.200:5060"},
            {"frame_number": 2, "timestamp": 1.2, "status_code": 180, "cseq_method": "INVITE", "src": "192.168.3.200:5060", "dst": "192.168.150.10:5060"},
            {"frame_number": 3, "timestamp": 2.0, "status_code": 200, "cseq_method": "INVITE", "src": "192.168.3.200:5060", "dst": "192.168.150.10:5060"},
            {"frame_number": 4, "timestamp": 2.01, "method": "ACK", "cseq_method": "ACK", "src": "192.168.150.10:5060", "dst": "192.168.3.200:5060"},
        ],
        "capture_completeness": {"has_invite_request": True, "has_invite_final_response": True, "has_ack_for_success": True},
    }


def _peer_call():
    return {
        "call_id": "leg-b",
        "caller": "sip:601@pbx",
        "callee": "sip:101@pbx",
        "state": "ESTABLISHED",
        "start_time": 1.05,
        "rtp_stream_ids": ["callee-down"],
        "ladder": [
            {"frame_number": 10, "timestamp": 1.05, "method": "INVITE", "cseq_method": "INVITE", "src": "192.168.3.200:5060", "dst": "192.168.150.12:5060"},
            {"frame_number": 11, "timestamp": 1.9, "status_code": 200, "cseq_method": "INVITE", "src": "192.168.150.12:5060", "dst": "192.168.3.200:5060"},
            {"frame_number": 12, "timestamp": 1.91, "method": "ACK", "cseq_method": "ACK", "src": "192.168.3.200:5060", "dst": "192.168.150.12:5060"},
        ],
        "capture_completeness": {"has_invite_request": True, "has_invite_final_response": True, "has_ack_for_success": True},
    }


def _packet():
    return {
        "calls": [_selected_call(), _peer_call()],
        "rtp_streams": [
            {"stream_id": "caller-up", "src_ip": "192.168.150.10", "dst_ip": "192.168.3.200", "packet_count": 100,
             "start_time": 2.1, "end_time": 5.0, "lost_packets": 0, "primary_call_id": "leg-a",
             "call_bindings": [{"call_id": "leg-a"}]},
            {"stream_id": "caller-down", "src_ip": "192.168.3.200", "dst_ip": "192.168.150.10", "packet_count": 100,
             "start_time": 2.11, "end_time": 5.01, "lost_packets": 0, "primary_call_id": "leg-a",
             "call_bindings": [{"call_id": "leg-a"}]},
            {"stream_id": "callee-down", "src_ip": "192.168.3.200", "dst_ip": "192.168.150.12", "packet_count": 100,
             "start_time": 2.12, "end_time": 5.02, "lost_packets": 0, "primary_call_id": "leg-b",
             "call_bindings": [{"call_id": "leg-b"}]},
        ],
        "anomalies": [
            {"type": "HIGH_DELTA", "time": 4.001, "evidence": {"stream_id": "caller-up", "call_id": "leg-a", "delta_ms": 42.1, "sequence_continuous": True}},
        ],
    }


def _pcm():
    return {
        "streams": [
            {"tap": {"name": "pcm_rx", "direction": "RX"}, "sessions": [{"start_time": 0.0, "end_time": 5.1, "gap_events": [
                {"time": 0.5, "delta_ms": 32.8}, {"time": 4.0, "delta_ms": 32.0},
            ]}]},
            {"tap": {"name": "pcm_tx", "direction": "TX"}, "sessions": [{"start_time": 0.0, "end_time": 5.1, "gap_events": [
                {"time": 0.501, "delta_ms": 32.9}, {"time": 4.002, "delta_ms": 32.1},
            ]}]},
        ]
    }


def test_runtime_adapter_keeps_pre_call_timing_as_uncertain_and_one_active_cluster():
    report = compose_v2_runtime_from_analyzers(
        report_id="R-GOLDEN-LIKE",
        sip_call=_selected_call(),
        packet=_packet(),
        pcm=_pcm(),
        media={"cross_layer_events": [{
            "type": "DTMF_SIP_DIAL_MATCH", "time": 0.8, "scope": {"call_id": "leg-a"},
            "details": {"pcm_digits": "101", "sip_target": "101", "match": True},
        }]},
        subject_device_ip="192.168.150.10",
    )

    assert report["call_reconstruction"]["state"] == "ESTABLISHED"
    assert report["call_reconstruction"]["termination"]["observed"] is False
    assert report["call_reconstruction"]["call_end_time"] is None
    assert report["timeline"]["media_observation_window"]["start"] == 2.1
    assert report["timeline"]["media_observation_window"]["end"] == 5.02
    assert report["logical_call"]["logical_call_count"] == 1
    assert report["logical_call"]["resolved_sip_leg_count"] == 2
    assert report["visibility"]["media"]["caller_leg"] == "BIDIRECTIONAL"
    assert report["visibility"]["media"]["callee_leg"] == "ONE_WAY"
    assert report["visibility"]["end_to_end_media"] == "PARTIAL"
    assert report["visibility"]["termination"] == "NOT_OBSERVED"

    pre_call = [item for item in report["findings"] if item.get("phase") == "PRE_CALL"]
    assert pre_call
    assert {item["class"] for item in pre_call} == {"UNCERTAIN"}

    assert len(report["correlation_clusters"]) == 1
    cluster = report["correlation_clusters"][0]
    assert cluster["type"] == "CROSS_LAYER_MEDIA_TIMING_SPIKE"
    assert cluster["member_layer_families"] == ["PCM", "RTP"]
    assert report["problem_count"] == 1
    assert report["semantic_validation"]["status"] == "PASS"
    assert report["publishable"] is True
    assert report["first_page"]["symptom_reproduction"] == "本次未复现"


def test_dtmf_absence_is_unknown_not_mismatch_or_reproduction():
    report = compose_v2_runtime_from_analyzers(
        report_id="R-NO-DTMF",
        sip_call=_selected_call(),
        packet=_packet(),
        pcm=_pcm(),
        media={"cross_layer_events": []},
        subject_device_ip="192.168.150.10",
    )
    assert report["symptom_assessment"] == {}
    assert report["first_page"]["symptom_reproduction"] == "无法确认"
