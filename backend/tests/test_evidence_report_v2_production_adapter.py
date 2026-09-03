import pytest

from app.services.evidence_report_v2 import compose_v2_runtime_payload


def _call(call_id="leg-a"):
    return {
        "call_id": call_id,
        "caller": "sip:601@pbx",
        "callee": "sip:101@pbx",
        "state": "ESTABLISHED",
        "start_time": 1.0,
        "ladder": [
            {"timestamp": 1.0, "method": "INVITE", "cseq_method": "INVITE", "src": "10.0.0.1:5060", "dst": "10.0.0.2:5060"},
            {"timestamp": 1.2, "status_code": 200, "cseq_method": "INVITE", "src": "10.0.0.2:5060", "dst": "10.0.0.1:5060"},
            {"timestamp": 1.21, "method": "ACK", "cseq_method": "ACK", "src": "10.0.0.1:5060", "dst": "10.0.0.2:5060"},
        ],
    }


def _results():
    packet = {
        "calls": [_call()],
        "rtp_streams": [{
            "stream_id": "s1", "src_ip": "10.0.0.1", "dst_ip": "10.0.0.2",
            "packet_count": 10, "start_time": 1.3, "end_time": 1.5,
            "lost_packets": 0, "primary_call_id": "leg-a", "call_bindings": [{"call_id": "leg-a"}],
        }],
        "anomalies": [],
    }
    pcm = {"streams": []}
    return {"packet_intelligence": packet, "pcm_intelligence": pcm, "media_intelligence": {"cross_layer_events": []}}


def test_production_v2_uses_analysis_context_selected_call():
    report = compose_v2_runtime_payload(
        report_id="R1",
        results=_results(),
        analysis_context={"selected_sip_call_id": "leg-a", "subject_device_ip": "10.0.0.1"},
    )
    assert report["schema"] == "preliminary-evidence-report-v2"
    assert report["call_reconstruction"]["call_id"] == "leg-a"
    assert report["timeline"]["media_observation_window"]["source"] == "RTP_OBSERVATION"
    assert report["publishable"] is True


def test_production_v2_fails_closed_when_selected_call_is_absent():
    with pytest.raises(ValueError, match="EVIDENCE_V2_SELECTED_SIP_CALL_NOT_FOUND"):
        compose_v2_runtime_payload(
            report_id="R1",
            results=_results(),
            analysis_context={"selected_sip_call_id": "missing"},
        )
