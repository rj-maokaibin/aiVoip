from __future__ import annotations

from app.analyzers.correlation import correlate_pcm_dtmf_with_sip


DUT_IP = "192.168.150.4"
PBX_IP = "192.168.3.200"
PEER_IP = "192.168.150.8"
DUT_CALL = "00ad1c804c33b255@192.168.3.200"
PBX_CALL = "pbx-leg@192.168.3.200"


def _call(call_id: str, start: float, offer_ip: str, answer_ip: str, stream_id: str) -> dict:
    return {
        "call_id": call_id,
        "start_time": start,
        "callee": "sip:601@192.168.3.200",
        "rtp_stream_ids": [stream_id],
        "sdp": {
            "offer": {"connection_address": offer_ip, "media": [{"connection_address": offer_ip}]},
            "answer": {"connection_address": answer_ip, "media": [{"connection_address": answer_ip}]},
        },
    }


def _packet() -> dict:
    return {
        "calls": [
            _call(DUT_CALL, 10.0, DUT_IP, PBX_IP, "dut-up"),
            _call(PBX_CALL, 10.1, PBX_IP, PEER_IP, "pbx-up"),
        ],
        "rtp_streams": [
            {"stream_id": "dut-up", "src_ip": DUT_IP, "dst_ip": PBX_IP},
            {"stream_id": "pbx-up", "src_ip": PBX_IP, "dst_ip": PEER_IP},
        ],
    }


def _pcm(source_endpoints: bool = True) -> dict:
    stream = {
        "tap": {"name": "pcm_rx", "direction": "RX"},
        "packet_count": 100,
        "sessions": [{
            "session_index": 0,
            "start_time": 0.0,
            "dtmf_sequences": [{"digits": "601", "start_seconds": 4.0, "end_seconds": 5.0, "min_confidence": 0.95}],
        }],
    }
    if source_endpoints:
        stream["source_endpoints"] = [{"ip": DUT_IP, "port": 48741, "packet_count": 100}]
    return {"streams": [stream]}


def test_dtmf_match_is_emitted_only_for_dut_facing_leg():
    events = correlate_pcm_dtmf_with_sip(_packet(), _pcm())
    assert len(events) == 1
    assert events[0]["type"] == "DTMF_SIP_DIAL_MATCH"
    assert events[0]["details"]["call_id"] == DUT_CALL
    assert events[0]["scope"]["call_id"] == DUT_CALL
    assert events[0]["details"]["subject_call_selection"]["status"] == "SUBJECT_CALL_SELECTED"


def test_multileg_without_pcm_source_identity_fails_closed_instead_of_duplicating_match():
    events = correlate_pcm_dtmf_with_sip(_packet(), _pcm(source_endpoints=False))
    assert events == []


def test_single_call_keeps_backward_compatible_dtmf_matching_without_source_identity():
    packet = _packet()
    packet["calls"] = [packet["calls"][0]]
    events = correlate_pcm_dtmf_with_sip(packet, _pcm(source_endpoints=False))
    assert len(events) == 1
    assert events[0]["details"]["call_id"] == DUT_CALL
