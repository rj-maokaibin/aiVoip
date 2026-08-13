from app.analyzers.packet.rtp import RtpStreamAnalyzer
from app.analyzers.packet.types import NormalizedPacket, RtpData


def pkt(frame, t, seq, ts, pt=8, ssrc=0x1234):
    return NormalizedPacket(
        frame_number=frame,
        timestamp=t,
        src_ip="10.0.0.1",
        dst_ip="10.0.0.2",
        transport="UDP",
        src_port=4000,
        dst_port=5000,
        rtp=RtpData(ssrc=ssrc, sequence=seq, timestamp=ts, payload_type=pt),
    )


def test_rtp_burst_loss_and_ptime():
    packets = [
        pkt(1, 1.00, 100, 0),
        pkt(2, 1.02, 101, 160),
        pkt(3, 1.10, 106, 960),
        pkt(4, 1.12, 107, 1120),
    ]
    result = RtpStreamAnalyzer().analyze(packets)[0]
    assert result["lost_packets"] == 4
    assert result["max_consecutive_loss"] == 4
    assert result["ptime_ms"] == 20
    event = next(x for x in result["events"] if x["type"] == "BURST_LOSS")
    assert event["details"]["estimated_audio_loss_ms"] == 80


def test_rtp_wrap_duplicate_and_reorder():
    packets = [
        pkt(1, 1.00, 65534, 0),
        pkt(2, 1.02, 65535, 160),
        pkt(3, 1.04, 0, 320),
        pkt(4, 1.06, 2, 640),
        pkt(5, 1.07, 1, 480),  # reordered arrival fills gap
        pkt(6, 1.08, 2, 640),  # duplicate
    ]
    result = RtpStreamAnalyzer().analyze(packets)[0]
    assert result["lost_packets"] == 0
    assert result["out_of_order_packets"] == 1
    assert result["duplicate_packets"] == 1


def test_unknown_dynamic_payload_does_not_guess_clock_rate_or_ptime():
    packets=[pkt(i, i*0.02, i, i*160, pt=110) for i in range(1,6)]
    result=RtpStreamAnalyzer().analyze(packets)[0]
    assert result['codec']=='PT110'
    assert result['clock_rate'] is None
    assert result['ptime_ms'] is None
    assert result['avg_jitter_ms'] is None
