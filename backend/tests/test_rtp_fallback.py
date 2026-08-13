import socket, struct
from pathlib import Path

from app.analyzers.packet.pcap_rtp_fallback import read_rtp_packets_fallback


def _udp_frame(seq:int, ts:int, ssrc:int=1234, pt:int=8):
    payload=bytes([0xD5])*160
    rtp=bytes([0x80,pt])+struct.pack('!HII',seq,ts,ssrc)+payload
    src=socket.inet_aton('10.0.0.1'); dst=socket.inet_aton('10.0.0.2')
    udp=struct.pack('!HHHH',10000,20000,8+len(rtp),0)+rtp
    ip_len=20+len(udp)
    ip=bytes([0x45,0])+struct.pack('!H',ip_len)+b'\x00\x00\x00\x00'+bytes([64,17])+b'\x00\x00'+src+dst
    eth=b'\x00'*12+b'\x08\x00'
    return eth+ip+udp


def _write_pcap(path:Path):
    with path.open('wb') as f:
        f.write(b'\xd4\xc3\xb2\xa1'+struct.pack('<HHiiii',2,4,0,0,65535,1))
        for i in range(25):
            data=_udp_frame(100+i,i*160)
            f.write(struct.pack('<IIII',1,i*20000,len(data),len(data))); f.write(data)


def test_restricted_rtp_fallback(tmp_path):
    p=tmp_path/'rtp.pcap'; _write_pcap(p)
    packets=read_rtp_packets_fallback(p,min_packets=20)
    assert len(packets)==25
    assert packets[0].rtp.payload_type==8
    assert packets[-1].rtp.sequence==124
