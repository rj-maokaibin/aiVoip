from __future__ import annotations

import math
import socket
import struct
from pathlib import Path

import numpy as np

from app.analyzers.pcm import PcmIntelligenceEngine, PcmProfile
from app.analyzers.pcm.profile import PcmTap


def _tone(digit: str, fs=8000, on_ms=100, off_ms=100):
    table={
        '8':(852,1336),'0':(941,1336),'3':(697,1477)
    }
    f1,f2=table[digit]
    n=int(fs*on_ms/1000)
    t=np.arange(n)/fs
    x=(3500*np.sin(2*np.pi*f1*t)+3500*np.sin(2*np.pi*f2*t)).astype('<i2')
    z=np.zeros(int(fs*off_ms/1000),dtype='<i2')
    return np.concatenate([x,z])


def _ether_ipv4_udp(src,dst,sp,dp,payload):
    eth=b'\x00'*12+struct.pack('!H',0x0800)
    udp_len=8+len(payload)
    total=20+udp_len
    ip=bytearray(20)
    ip[0]=0x45
    struct.pack_into('!H',ip,2,total)
    ip[8]=64; ip[9]=17
    ip[12:16]=socket.inet_aton(src); ip[16:20]=socket.inet_aton(dst)
    udp=struct.pack('!HHHH',sp,dp,udp_len,0)
    return eth+bytes(ip)+udp+payload


def _write_pcap(path:Path, packets:list[tuple[float,bytes]]):
    with path.open('wb') as f:
        f.write(b'\xd4\xc3\xb2\xa1')
        f.write(struct.pack('<HHiiii',2,4,0,0,262144,1))
        for ts,data in packets:
            sec=int(ts); usec=int(round((ts-sec)*1_000_000))
            f.write(struct.pack('<IIII',sec,usec,len(data),len(data)))
            f.write(data)


def test_pcm_profile_detects_dial_sequence(tmp_path):
    samples=np.concatenate([_tone('8'),_tone('8'),_tone('0'),_tone('3')])
    packets=[]
    raw=samples.tobytes()
    for i in range(0,len(raw),160):
        chunk=raw[i:i+160]
        assert len(chunk)==160
        packets.append((1000.0+(i//160)*0.010,_ether_ipv4_udp('192.168.0.12','192.168.0.253',32984,40000,chunk)))
    pcap=tmp_path/'dtmf.pcap'; _write_pcap(pcap,packets)
    profile=PcmProfile(id='test',sample_rate=8000,bit_depth=16,signed=True,endian='little',channels=1,
                       packet_payload_bytes=160,expected_packet_interval_ms=10,session_gap_ms=100,
                       taps=[PcmTap(name='pcm_rx',direction='RX',dst_port=40000)])
    result=PcmIntelligenceEngine(profile).analyze_pcap(pcap)
    session=result['streams'][0]['sessions'][0]
    assert session['dtmf_sequences'][0]['digits']=='8803'
    assert session['median_packet_interval_ms']==10.0
    assert session['signal']['clipping_percent']==0.0


def test_pcm_sessions_split_on_large_gap(tmp_path):
    silence=np.zeros(80,dtype='<i2').tobytes()
    packets=[]
    for ts in [1.00,1.01,1.02,2.00,2.01]:
        packets.append((ts,_ether_ipv4_udp('1.1.1.1','2.2.2.2',1000,50000,silence)))
    pcap=tmp_path/'split.pcap'; _write_pcap(pcap,packets)
    profile=PcmProfile(id='test',sample_rate=8000,bit_depth=16,signed=True,endian='little',channels=1,
                       packet_payload_bytes=160,expected_packet_interval_ms=10,session_gap_ms=100,
                       taps=[PcmTap(name='pcm_tx',direction='TX',dst_port=50000)])
    result=PcmIntelligenceEngine(profile).analyze_pcap(pcap)
    assert result['summary']['session_count']==2
