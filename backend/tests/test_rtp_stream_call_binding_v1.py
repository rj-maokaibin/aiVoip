from __future__ import annotations

from app.analyzers.packet.engine import PacketIntelligenceEngine


def _call(call_id: str, offer_ip: str, offer_port: int, answer_ip: str, answer_port: int, stream_ids: list[str]) -> dict:
    return {
        "call_id": call_id,
        "rtp_stream_ids": stream_ids,
        "sdp": {
            "offer": {"media": [{"media": "audio", "connection_address": offer_ip, "port": offer_port}]},
            "answer": {"media": [{"media": "audio", "connection_address": answer_ip, "port": answer_port}]},
        },
    }


def _stream(stream_id: str, src_ip: str, src_port: int, dst_ip: str, dst_port: int) -> dict:
    return {
        "stream_id": stream_id,
        "src_ip": src_ip,
        "src_port": src_port,
        "dst_ip": dst_ip,
        "dst_port": dst_port,
    }


def test_stream_gets_single_call_and_direction_role_when_binding_is_unique():
    up = _stream("up", "192.168.150.4", 10000, "192.168.3.200", 11446)
    down = _stream("down", "192.168.3.200", 11446, "192.168.150.4", 10000)
    calls = [_call("dut-call", "192.168.150.4", 10000, "192.168.3.200", 11446, ["up", "down"])]

    PacketIntelligenceEngine()._attach_stream_call_bindings(calls, [up, down])

    assert up["primary_call_id"] == "dut-call"
    assert up["call_direction_role"] == "OFFERER_TO_ANSWERER"
    assert down["primary_call_id"] == "dut-call"
    assert down["call_direction_role"] == "ANSWERER_TO_OFFERER"
    assert up["call_bindings"][0]["offer_audio_endpoint"] == {"ip": "192.168.150.4", "port": 10000}
    assert up["call_bindings"][0]["answer_audio_endpoint"] == {"ip": "192.168.3.200", "port": 11446}


def test_b2bua_multi_call_binding_is_preserved_instead_of_guessing_primary_call():
    shared = _stream("shared", "10.0.0.1", 10000, "10.0.0.2", 20000)
    calls = [
        _call("leg-a", "10.0.0.1", 10000, "10.0.0.2", 20000, ["shared"]),
        _call("leg-b", "10.0.0.1", 10000, "10.0.0.2", 20000, ["shared"]),
    ]

    PacketIntelligenceEngine()._attach_stream_call_bindings(calls, [shared])

    assert shared["primary_call_id"] is None
    assert shared["call_direction_role"] == "MULTI_CALL_BOUND"
    assert {x["call_id"] for x in shared["call_bindings"]} == {"leg-a", "leg-b"}
