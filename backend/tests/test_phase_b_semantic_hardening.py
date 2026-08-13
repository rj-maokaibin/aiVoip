from __future__ import annotations

import socket
import struct
from pathlib import Path

from app.analyzers.packet.rtcp import analyze_rtcp
from app.analyzers.packet.rtp import RtpPtimeHint, RtpStreamAnalyzer
from app.analyzers.packet.types import NormalizedPacket, RtcpData, RtpData
from app.analyzers.pcm import PcmIntelligenceEngine
from app.analyzers.pcm.profile import PcmProfile, PcmTap, load_pcm_profile
from app.analyzers.profile import get_default_analyzer_profile, load_analyzer_profile


def _rtp(frame: int, t: float, seq: int, ts: int, *, pt: int = 8) -> NormalizedPacket:
    return NormalizedPacket(
        frame_number=frame,
        timestamp=t,
        src_ip="10.0.0.1",
        dst_ip="10.0.0.2",
        transport="UDP",
        src_port=4000,
        dst_port=5000,
        protocols=["rtp"],
        rtp=RtpData(ssrc=1234, sequence=seq, timestamp=ts, payload_type=pt),
    )


def _ether_ipv4_udp(src: str, dst: str, sp: int, dp: int, payload: bytes) -> bytes:
    eth=b'\x00'*12+struct.pack('!H',0x0800)
    udp_len=8+len(payload); total=20+udp_len
    ip=bytearray(20); ip[0]=0x45; struct.pack_into('!H',ip,2,total); ip[8]=64; ip[9]=17
    ip[12:16]=socket.inet_aton(src); ip[16:20]=socket.inet_aton(dst)
    udp=struct.pack('!HHHH',sp,dp,udp_len,0)
    return eth+bytes(ip)+udp+payload


def _write_pcap(path: Path, packets: list[tuple[float, bytes]]) -> None:
    with path.open('wb') as f:
        f.write(b'\xd4\xc3\xb2\xa1')
        f.write(struct.pack('<HHiiii',2,4,0,0,262144,1))
        for ts,data in packets:
            sec=int(ts); usec=int(round((ts-sec)*1_000_000))
            f.write(struct.pack('<IIII',sec,usec,len(data),len(data))); f.write(data)


def test_analyzer_profile_is_versioned_calibrated_and_checksummed():
    profile=get_default_analyzer_profile()
    assert profile.id == 'voip_analyzer_v1'
    assert profile.version == '1.0.0'
    assert profile.status == 'GOLDEN_CALIBRATED'
    assert profile.confirmable is True
    assert len(profile.checksum) == 64
    reloaded=load_analyzer_profile(profile.source_path)
    assert reloaded.checksum == profile.checksum


def test_rtp_sdp_ptime_hint_has_priority_over_timestamp_inference():
    # RTP timestamps imply 20 ms but SDP says 30 ms. Contract requires SDP priority.
    packets=[_rtp(1,1.00,100,0),_rtp(2,1.03,101,160),_rtp(3,1.06,102,320)]
    hint=RtpPtimeHint(timestamp=0.5,ip='10.0.0.2',port=5000,payload_types=(8,),ptime_ms=30.0)
    result=RtpStreamAnalyzer(ptime_hints=[hint]).analyze(packets)[0]
    assert result['ptime_ms'] == 30.0
    assert result['ptime_source'] == 'SDP'
    assert result['availability']['ptime'] == 'AVAILABLE'


def test_unknown_dynamic_pt_explicitly_marks_semantics_unavailable():
    packets=[_rtp(i,i*0.02,i,i*160,pt=110) for i in range(1,6)]
    result=RtpStreamAnalyzer().analyze(packets)[0]
    assert result['clock_rate'] is None
    assert result['ptime_ms'] is None
    assert result['availability']['clock_rate'] == 'UNAVAILABLE'
    assert result['availability']['rfc3550_jitter'] == 'UNAVAILABLE'
    assert result['availability']['estimated_audio_loss'] == 'UNAVAILABLE'


def test_pcm_raw_profile_preserves_packets_but_blocks_audio_semantics(tmp_path):
    payload=b'\x01\x02'*80
    pcap=tmp_path/'raw.pcap'
    _write_pcap(pcap,[(1000.0,_ether_ipv4_udp('1.1.1.1','2.2.2.2',1111,40000,payload))])
    profile=PcmProfile.raw(id='raw-test',taps=[PcmTap(name='pcm_rx',direction='RX',dst_port=40000)],packet_payload_bytes=160)
    result=PcmIntelligenceEngine(profile).analyze_pcap(pcap)
    assert result['status'] == 'PARTIAL_SUCCESS'
    assert result['format_availability'] == 'UNAVAILABLE'
    session=result['streams'][0]['sessions'][0]
    assert session['analysis_availability'] == 'UNAVAILABLE'
    assert session['unavailable_reason'] == 'PCM_FORMAT_NOT_VERIFIED'
    assert 'dtmf_events' not in session


def test_pcm_verified_profile_contains_transport_offset_contract():
    root=Path(__file__).resolve().parents[2]
    profile=load_pcm_profile(root/'profiles/pcm/ruijie_aim_diag_v1.yaml')
    assert profile.can_decode is True
    assert profile.header_length == 0
    assert profile.payload_offset == 0
    assert profile.decoded_payload_bytes == 160
    assert len(profile.checksum or '') == 64


def test_rtcp_relative_capture_does_not_invent_rtt():
    packet=NormalizedPacket(
        frame_number=1,timestamp=12.0,src_ip='1.1.1.1',dst_ip='2.2.2.2',protocols=['rtcp'],
        rtcp=RtcpData(packet_type='201',ssrc=1,fraction_lost=2,cumulative_lost=3,jitter=160,lsr=100,dlsr=50),
    )
    result=analyze_rtcp([packet])[0]
    assert result['report_type'] == 'RR'
    assert result['rtt_seconds'] is None
    assert result['availability']['rtt'] == 'UNAVAILABLE'
