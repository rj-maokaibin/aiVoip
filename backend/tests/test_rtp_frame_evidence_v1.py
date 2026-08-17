from app.analyzers.packet.rtp import RtpStreamAnalyzer
from app.analyzers.packet.types import NormalizedPacket, RtpData


def _pkt(frame:int,time_s:float,seq:int,ts:int,pt:int=0):
    return NormalizedPacket(frame_number=frame,timestamp=time_s,src_ip="10.0.0.1",dst_ip="10.0.0.2",transport="udp",src_port=10000,dst_port=20000,
                            rtp=RtpData(ssrc=1234,sequence=seq,timestamp=ts,payload_type=pt,payload_size=160))


def test_rtp_loss_event_contains_boundary_frames_and_stream_frame_range():
    streams=RtpStreamAnalyzer().analyze([
        _pkt(10,0.00,1,0),
        _pkt(11,0.02,2,160),
        _pkt(30,0.10,5,640),
    ])
    assert len(streams)==1
    stream=streams[0]
    assert stream["first_frame_number"]==10
    assert stream["last_frame_number"]==30
    loss=next(e for e in stream["events"] if e["type"]=="BURST_LOSS")
    assert loss["details"]["lost_packets"]==2
    assert loss["details"]["previous_frame_number"]==11
    assert loss["details"]["next_frame_number"]==30
    assert loss["details"]["missing_sequence_ext_start"]==3
    assert loss["details"]["missing_sequence_ext_end"]==4
    assert "没有可引用 Frame" in loss["details"]["frame_evidence_note"]


def test_rtp_high_delta_contains_previous_and_current_frames():
    streams=RtpStreamAnalyzer().analyze([
        _pkt(100,0.00,1,0),
        _pkt(101,0.02,2,160),
        _pkt(120,0.12,3,320),
        _pkt(121,0.14,4,480),
    ])
    event=next(e for e in streams[0]["events"] if e["type"]=="HIGH_DELTA")
    assert event["details"]["previous_frame_number"]==101
    assert event["details"]["current_frame_number"]==120
    assert event["details"]["previous_sequence"]==2
    assert event["details"]["current_sequence"]==3
