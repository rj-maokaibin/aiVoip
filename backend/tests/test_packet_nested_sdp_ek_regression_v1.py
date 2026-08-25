from __future__ import annotations

from app.analyzers.correlation import correlate_pcm_dtmf_with_sip
from app.analyzers.packet.engine import PacketIntelligenceEngine
from app.analyzers.packet.normalize import FieldIndex, normalize_ek_record
from app.analyzers.packet.sip import reconstruct_sip


DUT_IP = "192.168.150.4"
PBX_IP = "192.168.3.200"
PEER_IP = "192.168.150.8"
DUT_CALL = "00ad1c804c33b255@192.168.3.200"
PBX_CALL = "pbx-leg@192.168.3.200"


def _record(
    *,
    frame: int,
    ts: float,
    src: str,
    dst: str,
    call_id: str,
    method: str | None = None,
    status: int | None = None,
    cseq_method: str = "INVITE",
    from_uri: str = "sip:8000@192.168.3.200;user=phone",
    to_uri: str = "sip:601@192.168.3.200;user=phone",
    sdp_ip: str | None = None,
    sdp_port: int | None = None,
    payloads: str = "8 0 4 18",
) -> dict:
    sip = {
        "sip_sip_Call-ID": call_id,
        "sip_sip_CSeq_seq": "1",
        "sip_sip_CSeq_method": cseq_method,
        "sip_sip_from_addr": from_uri,
        "sip_sip_to_addr": to_uri,
    }
    if method:
        sip["sip_sip_Method"] = method
        request_uri = to_uri.replace("sip:", "sip:", 1)
        sip["sip_sip_Request-Line"] = f"{method} {request_uri} SIP/2.0"
    if status is not None:
        sip["sip_sip_Status-Code"] = str(status)
        sip["sip_sip_Status-Line"] = f"SIP/2.0 {status} OK"
    if sdp_ip is not None and sdp_port is not None:
        # Wireshark/TShark 4.2 real EK shape: SDP is nested under layers.sip.sdp,
        # not exposed as a top-level layers.sdp protocol object.
        sip["sdp"] = {
            "sdp_sdp_connection_info_address": sdp_ip,
            "sdp_sdp_media": f"audio {sdp_port} RTP/AVP {payloads}",
            "sdp_sdp_media_port": str(sdp_port),
            "sdp_sdp_media_proto": "RTP/AVP",
            "sdp_sdp_media_attr": "sendrecv",
        }
    return {
        "layers": {
            "sip": sip,
            "udp": {"udp_udp_srcport": "5060", "udp_udp_dstport": "5060"},
            "ip": {"ip_ip_src": src, "ip_ip_dst": dst},
            "frame": {
                "frame_frame_number": str(frame),
                "frame_frame_time_epoch": str(ts),
                "frame_frame_protocols": "eth:ip:udp:sip:sdp" if sdp_ip else "eth:ip:udp:sip",
            },
        }
    }


def _normalize(*records: dict):
    packets = [normalize_ek_record(record) for record in records]
    assert all(packet is not None for packet in packets)
    return packets


def _pcm_601() -> dict:
    return {
        "streams": [{
            "tap": {"name": "pcm_rx", "direction": "RX", "dst_port": 40000},
            "packet_count": 6525,
            "source_endpoints": [{"ip": DUT_IP, "port": 48741, "packet_count": 6525}],
            "sessions": [{
                "session_index": 0,
                "start_time": 1786690955.283755,
                "dtmf_sequences": [{
                    "digits": "601",
                    "start_seconds": 9.04,
                    "end_seconds": 9.92,
                    "event_count": 3,
                    "min_confidence": 0.866241,
                }],
            }],
        }]
    }


def test_nested_sdp_protocol_is_preserved_from_real_tshark_ek_shape():
    raw = _record(
        frame=2786,
        ts=1786690969.100710,
        src=DUT_IP,
        dst=PBX_IP,
        call_id=DUT_CALL,
        method="INVITE",
        sdp_ip=DUT_IP,
        sdp_port=10000,
    )
    idx = FieldIndex(raw["layers"])
    assert idx.has_layer("sip") is True
    assert idx.has_layer("sdp") is True

    packet = normalize_ek_record(raw)
    assert packet is not None
    assert packet.sdp is not None
    assert packet.sdp.connection_address == DUT_IP
    assert packet.sdp.media_port == 10000
    assert packet.sdp.media_protocol == "RTP/AVP"
    assert packet.sdp.media_payload_types == [8, 0, 4, 18]
    assert "sendrecv" in packet.sdp.attributes
    assert "sdp" in packet.protocols


def test_nested_sdp_drives_bidirectional_rtp_binding_and_pcm_601_subject_call_match():
    dut_packets = _normalize(
        _record(
            frame=2786,
            ts=1786690969.100710,
            src=DUT_IP,
            dst=PBX_IP,
            call_id=DUT_CALL,
            method="INVITE",
            sdp_ip=DUT_IP,
            sdp_port=10000,
        ),
        _record(
            frame=3387,
            ts=1786690972.052840,
            src=PBX_IP,
            dst=DUT_IP,
            call_id=DUT_CALL,
            status=200,
            sdp_ip=PBX_IP,
            sdp_port=11446,
            payloads="0 4",
        ),
        _record(
            frame=3390,
            ts=1786690972.055640,
            src=DUT_IP,
            dst=PBX_IP,
            call_id=DUT_CALL,
            method="ACK",
            cseq_method="ACK",
        ),
        _record(
            frame=20412,
            ts=1786691020.535864,
            src=DUT_IP,
            dst=PBX_IP,
            call_id=DUT_CALL,
            method="BYE",
            cseq_method="BYE",
        ),
    )
    pbx_leg = _normalize(
        _record(
            frame=2808,
            ts=1786690969.195511,
            src=PBX_IP,
            dst=PEER_IP,
            call_id=PBX_CALL,
            method="INVITE",
            to_uri=f"sip:601@{PEER_IP}:5060;user=phone",
            sdp_ip=PBX_IP,
            sdp_port=11448,
            payloads="0 4",
        )
    )

    calls = reconstruct_sip([*dut_packets, *pbx_leg])["calls"]
    dut_call = next(call for call in calls if call["call_id"] == DUT_CALL)
    assert dut_call["sdp"]["offer"]["connection_address"] == DUT_IP
    assert dut_call["sdp"]["answer"]["connection_address"] == PBX_IP

    streams = [
        {
            "stream_id": "dut-up",
            "src_ip": DUT_IP,
            "src_port": 10000,
            "dst_ip": PBX_IP,
            "dst_port": 11446,
            "packet_count": 2423,
            "start_time": 1786690972.086401,
            "end_time": 1786691020.134777,
            "codec": "PCMU",
        },
        {
            "stream_id": "dut-down",
            "src_ip": PBX_IP,
            "src_port": 11446,
            "dst_ip": DUT_IP,
            "dst_port": 10000,
            "packet_count": 2425,
            "start_time": 1786690972.086401,
            "end_time": 1786691020.134777,
            "codec": "PCMU",
        },
    ]
    engine = PacketIntelligenceEngine()
    engine._attach_streams_to_calls(calls, streams)
    engine._attach_stream_call_bindings(calls, streams)
    engine._attach_media_direction_health(calls, streams)

    assert set(dut_call["rtp_stream_ids"]) == {"dut-up", "dut-down"}
    assert dut_call["media_direction_health"]["status"] == "BIDIRECTIONAL"
    assert dut_call["media_direction_health"]["endpoint_a"] == {"ip": DUT_IP, "port": 10000}
    assert dut_call["media_direction_health"]["endpoint_b"] == {"ip": PBX_IP, "port": 11446}

    packet_result = {"calls": calls, "rtp_streams": streams}
    events = correlate_pcm_dtmf_with_sip(packet_result, _pcm_601())
    assert len(events) == 1
    event = events[0]
    assert event["type"] == "DTMF_SIP_DIAL_MATCH"
    assert event["scope"]["call_id"] == DUT_CALL
    assert event["details"]["call_id"] == DUT_CALL
    assert event["details"]["pcm_digits"] == "601"
    assert event["details"]["sip_target"] == "601"
    assert event["details"]["subject_call_selection"]["status"] == "SUBJECT_CALL_SELECTED"
