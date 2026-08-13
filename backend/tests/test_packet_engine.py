from app.analyzers.packet.engine import PacketIntelligenceEngine
from app.analyzers.packet.types import NormalizedPacket, RtpData, SdpData, SipData

OFFER = """v=0\r\nc=IN IP4 10.0.0.1\r\nm=audio 4000 RTP/AVP 8 0\r\na=rtpmap:8 PCMA/8000\r\na=rtpmap:0 PCMU/8000\r\na=ptime:20\r\n"""
ANSWER = """v=0\r\nc=IN IP4 10.0.0.2\r\nm=audio 5000 RTP/AVP 8\r\na=rtpmap:8 PCMA/8000\r\na=ptime:20\r\n"""


def sip(frame, t, src, dst, method=None, status=None, cseq_method=None, sdp=None):
    return NormalizedPacket(
        frame_number=frame, timestamp=t, src_ip=src, dst_ip=dst, transport="UDP", src_port=5060, dst_port=5060,
        sip=SipData(method=method, status_code=status, call_id="call-1", cseq=1, cseq_method=cseq_method or method,
                    from_uri="sip:1001@pbx", to_uri="sip:1002@pbx"),
        sdp=SdpData(raw=sdp) if sdp else None,
    )


def rtp(frame, t, src, sp, dst, dp, seq, ts, pt=8, ssrc=1):
    return NormalizedPacket(frame_number=frame, timestamp=t, src_ip=src, dst_ip=dst, transport="UDP", src_port=sp, dst_port=dp,
                            rtp=RtpData(ssrc=ssrc, sequence=seq, timestamp=ts, payload_type=pt))


def test_engine_reconstructs_call_sdp_rtp_and_loss():
    packets = [
        sip(1, 1.0, "10.0.0.1", "10.0.0.2", method="INVITE", sdp=OFFER),
        sip(2, 1.1, "10.0.0.2", "10.0.0.1", status=180, cseq_method="INVITE"),
        sip(3, 1.2, "10.0.0.2", "10.0.0.1", status=200, cseq_method="INVITE", sdp=ANSWER),
        sip(4, 1.21, "10.0.0.1", "10.0.0.2", method="ACK", cseq_method="INVITE"),
        rtp(5, 1.3, "10.0.0.1", 4000, "10.0.0.2", 5000, 10, 0),
        rtp(6, 1.32, "10.0.0.1", 4000, "10.0.0.2", 5000, 11, 160),
        rtp(7, 1.40, "10.0.0.1", 4000, "10.0.0.2", 5000, 16, 960),
    ]
    result = PacketIntelligenceEngine().analyze_packets(packets)
    assert result["summary"]["call_count"] == 1
    call = result["calls"][0]
    assert call["state"] == "ESTABLISHED"
    assert [c["name"] for c in call["sdp"]["negotiated_codecs"]] == ["PCMA"]
    assert len(call["rtp_stream_ids"]) == 1
    assert result["rtp_streams"][0]["lost_packets"] == 4
    assert any(x["type"] == "BURST_LOSS" for x in result["anomalies"])

def test_engine_detects_negotiated_vs_actual_codec_mismatch():
    packets = [
        sip(1, 1.0, "10.0.0.1", "10.0.0.2", method="INVITE", sdp=OFFER),
        sip(2, 1.2, "10.0.0.2", "10.0.0.1", status=200, cseq_method="INVITE", sdp=ANSWER),
        sip(3, 1.21, "10.0.0.1", "10.0.0.2", method="ACK", cseq_method="INVITE"),
        rtp(4, 1.30, "10.0.0.1", 4000, "10.0.0.2", 5000, 1, 0, pt=0),
        rtp(5, 1.32, "10.0.0.1", 4000, "10.0.0.2", 5000, 2, 160, pt=0),
    ]
    result = PacketIntelligenceEngine().analyze_packets(packets)
    call=result['calls'][0]
    assert call['sdp']['codec_mismatch'] is True
    assert 'PCMU' in call['sdp']['unexpected_actual_codecs']
    assert any(x['type']=='CODEC_NEGOTIATION_MISMATCH' for x in result['anomalies'])
