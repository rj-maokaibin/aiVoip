from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from typing import Iterable

import numpy as np

from app.reproduction.pcap_codec import PcapRecord, build_pcap, build_rtp, udp_ethernet_frame


def _alaw_encode_sample(sample: int) -> int:
    # ITU-T G.711 A-law encoder, deterministic and sufficient for test fixtures.
    pcm = int(max(-32768, min(32767, sample)))
    mask = 0xD5
    if pcm < 0:
        mask = 0x55
        pcm = -pcm - 1
    if pcm > 32635: pcm = 32635
    if pcm >= 256:
        seg = min(7, pcm.bit_length() - 8)
        aval = seg << 4 | ((pcm >> (seg + 3)) & 0x0F)
    else:
        aval = pcm >> 4
    return aval ^ mask


def alaw_encode(samples: np.ndarray) -> bytes:
    return bytes(_alaw_encode_sample(int(x)) for x in samples.astype(np.int16, copy=False))


def _normal_signal(n: int, sr: int, *, phase: float = 0.0) -> np.ndarray:
    t = np.arange(n, dtype=np.float64) / sr
    x = 1800*np.sin(2*math.pi*437*t + phase) + 650*np.sin(2*math.pi*733*t + phase*0.7)
    return np.clip(x, -12000, 12000).astype('<i2')


def _other_signal(n:int,sr:int,*,phase:float=0.0)->np.ndarray:
    t=np.arange(n,dtype=np.float64)/sr
    x=1500*np.sin(2*math.pi*997*t+phase)+500*np.sin(2*math.pi*1301*t+0.4)
    return np.clip(x,-12000,12000).astype('<i2')


def _periodic_signal(n: int, sr: int) -> np.ndarray:
    t = np.arange(n, dtype=np.float64) / sr
    # 50 Hz square-like waveform -> 20 ms period, negative ~10 ms ACF and odd 50-Hz harmonics.
    base = np.sign(np.sin(2*math.pi*50*t))
    x = 1100*base + 90*np.sin(2*math.pi*437*t)
    return np.clip(x, -5000, 5000).astype('<i2')


def _dtmf_signal(n: int, sr: int, digit: str = '1') -> np.ndarray:
    grid={'1':(697,1209),'2':(697,1336),'3':(697,1477),'4':(770,1209),'5':(770,1336),'6':(770,1477),'7':(852,1209),'8':(852,1336),'9':(852,1477),'0':(941,1336),'*':(941,1209),'#':(941,1477)}
    f1,f2=grid.get(digit,(697,1209)); t=np.arange(n,dtype=np.float64)/sr
    return np.clip(2600*np.sin(2*math.pi*f1*t)+2600*np.sin(2*math.pi*f2*t),-10000,10000).astype('<i2')


def _sip_message(start: str, call_id: str, cseq: str, *, from_uri: str='sip:1001@mock', to_uri: str='sip:1002@mock', body: str|None=None, from_tag: str='a1', to_tag: str|None=None) -> bytes:
    headers=[start,'Via: SIP/2.0/UDP mock;branch=z9hG4bK-mock',f'From: <{from_uri}>;tag={from_tag}',f'To: <{to_uri}>' + (f';tag={to_tag}' if to_tag else ''),f'Call-ID: {call_id}',f'CSeq: {cseq}']
    if body is not None:
        headers += ['Content-Type: application/sdp', f'Content-Length: {len(body.encode())}']
    else:
        headers += ['Content-Length: 0']
    return ('\r\n'.join(headers)+'\r\n\r\n'+(body or '')).encode()


@dataclass(frozen=True)
class MockCallScenario:
    pcap: bytes
    debug_log: bytes


class MockCallCaptureBuilder:
    sample_rate=8000
    packet_ms=20
    pcm_packet_ms=10

    def build(
        self, *, start_ms: int, end_ms: int, device_ip: str, gateway_ip: str, call_id: str,
        target_findings: Iterable[str] = (), verdict: str = 'NO_MATCH', profile_id: str = 'VOIP_GENERIC_FULL_CAPTURE',
    ) -> MockCallScenario:
        findings=set(target_findings); duration_ms=max(1200, end_ms-start_ms)
        duration_s=duration_ms/1000.0; sr=self.sample_rate; n=max(sr, int(sr*duration_s))
        is_target=str(verdict)=='MATCH'
        periodic=is_target and 'PERIODIC_INTERFERENCE' in findings
        dtmf=is_target and ('DTMF_PATH' in findings or profile_id=='DTMF_LOSS')
        echo=is_target and ('ECHO_PATH' in findings or profile_id=='ECHO')
        one_way=is_target and (profile_id=='ONE_WAY_AUDIO' or 'ONE_WAY_RTP_MEDIA' in findings)
        burst=is_target and (profile_id=='AUDIO_STUTTER' or 'RTP_BURST_LOSS' in findings)
        call_fail=is_target and profile_id=='CALL_SETUP_FAILURE'

        rx = _periodic_signal(n,sr) if periodic else _dtmf_signal(n,sr) if dtmf else _normal_signal(n,sr)
        tx = _other_signal(n,sr,phase=0.9)
        if echo:
            delay=int(0.12*sr); tx=np.zeros_like(rx); tx[delay:]=rx[:-delay]
        up = rx.copy()
        down = _other_signal(n,sr,phase=1.7)

        base=1_700_000_000.0 + start_ms/1000.0
        offer=f'v=0\r\nc=IN IP4 {device_ip}\r\nm=audio 4000 RTP/AVP 8\r\na=rtpmap:8 PCMA/8000\r\na=ptime:20\r\na=sendrecv\r\n'
        answer=f'v=0\r\nc=IN IP4 {gateway_ip}\r\nm=audio 5000 RTP/AVP 8\r\na=rtpmap:8 PCMA/8000\r\na=ptime:20\r\na=sendrecv\r\n'
        records=[]; ident=1
        inv=_sip_message(f'INVITE sip:1002@{gateway_ip} SIP/2.0',call_id,'1 INVITE',body=offer)
        records.append(PcapRecord(base,udp_ethernet_frame(device_ip,gateway_ip,5060,5060,inv,ident=ident))); ident+=1
        if call_fail:
            msg=_sip_message('SIP/2.0 404 Not Found',call_id,'1 INVITE',to_tag='b1')
            records.append(PcapRecord(base+0.08,udp_ethernet_frame(gateway_ip,device_ip,5060,5060,msg,ident=ident))); ident+=1
            return MockCallScenario(build_pcap(records), f'{start_ms} SIP_INVITE\n{start_ms+80} SIP_404\n'.encode())
        ok=_sip_message('SIP/2.0 200 OK',call_id,'1 INVITE',body=answer,to_tag='b1')
        ack=_sip_message(f'ACK sip:1002@{gateway_ip} SIP/2.0',call_id,'1 ACK',to_tag='b1')
        records += [PcapRecord(base+0.08,udp_ethernet_frame(gateway_ip,device_ip,5060,5060,ok,ident=ident)),PcapRecord(base+0.10,udp_ethernet_frame(device_ip,gateway_ip,5060,5060,ack,ident=ident+1))]; ident+=2

        # PCM diagnostic UDP packets: 80 signed-16 LE samples every 10 ms.
        pcm_samples=80; pcm_packets=max(1,n//pcm_samples)
        for i in range(pcm_packets):
            a=i*pcm_samples; b=min(n,a+pcm_samples)
            if b-a < pcm_samples: break
            t=base+0.12+i*0.01
            records.append(PcapRecord(t,udp_ethernet_frame(device_ip,gateway_ip,41000,40000,rx[a:b].astype('<i2').tobytes(),ident=ident))); ident+=1
            records.append(PcapRecord(t+0.0002,udp_ethernet_frame(device_ip,gateway_ip,41001,50000,tx[a:b].astype('<i2').tobytes(),ident=ident))); ident+=1

        rtp_samples=160; count=max(1,n//rtp_samples); seq_up=100; seq_down=200; ts_up=0; ts_down=0
        for i in range(count):
            a=i*rtp_samples; b=min(n,a+rtp_samples)
            if b-a < rtp_samples: break
            if burst and i in {12,13,14,15}:
                seq_up += 1; ts_up += rtp_samples
                continue
            t=base+0.14+i*0.02
            payload=alaw_encode(up[a:b]); rtp=build_rtp(payload,sequence=seq_up,timestamp=ts_up,ssrc=0x11111111,payload_type=8)
            records.append(PcapRecord(t,udp_ethernet_frame(device_ip,gateway_ip,4000,5000,rtp,ident=ident))); ident+=1
            seq_up+=1; ts_up+=rtp_samples
            if not one_way:
                payload2=alaw_encode(down[a:b]); rtp2=build_rtp(payload2,sequence=seq_down,timestamp=ts_down,ssrc=0x22222222,payload_type=8)
                records.append(PcapRecord(t+0.001,udp_ethernet_frame(gateway_ip,device_ip,5000,4000,rtp2,ident=ident))); ident+=1
                seq_down+=1; ts_down+=rtp_samples
        bye_t=base+0.15+duration_s
        bye=_sip_message(f'BYE sip:1002@{gateway_ip} SIP/2.0',call_id,'2 BYE',to_tag='b1')
        bye_ok=_sip_message('SIP/2.0 200 OK',call_id,'2 BYE',to_tag='b1')
        records += [PcapRecord(bye_t,udp_ethernet_frame(device_ip,gateway_ip,5060,5060,bye,ident=ident)),PcapRecord(bye_t+0.02,udp_ethernet_frame(gateway_ip,device_ip,5060,5060,bye_ok,ident=ident+1))]
        log=(f'{start_ms} FXS_OFFHOOK\n{start_ms+10} SIP_INVITE call={call_id}\n{end_ms} FXS_ONHOOK\n').encode()
        return MockCallScenario(build_pcap(records),log)

    def live_probe(self, *, start_ms:int, device_ip:str, gateway_ip:str, call_id:str) -> MockCallScenario:
        sr=self.sample_rate; n=int(sr*0.4); x=_normal_signal(n,sr); base=1_700_000_000.0+start_ms/1000.0; records=[]; ident=1
        offer=f'v=0\r\nc=IN IP4 {device_ip}\r\nm=audio 4000 RTP/AVP 8\r\na=rtpmap:8 PCMA/8000\r\na=ptime:20\r\na=sendrecv\r\n'
        answer=f'v=0\r\nc=IN IP4 {gateway_ip}\r\nm=audio 5000 RTP/AVP 8\r\na=rtpmap:8 PCMA/8000\r\na=ptime:20\r\na=sendrecv\r\n'
        for t,src,dst,msg in [
            (base,device_ip,gateway_ip,_sip_message(f'INVITE sip:1002@{gateway_ip} SIP/2.0',call_id,'1 INVITE',body=offer)),
            (base+0.08,gateway_ip,device_ip,_sip_message('SIP/2.0 200 OK',call_id,'1 INVITE',body=answer,to_tag='b1')),
            (base+0.10,device_ip,gateway_ip,_sip_message(f'ACK sip:1002@{gateway_ip} SIP/2.0',call_id,'1 ACK',to_tag='b1')),
        ]:
            records.append(PcapRecord(t,udp_ethernet_frame(src,dst,5060,5060,msg,ident=ident))); ident+=1
        for i in range(20):
            a=i*160; payload=alaw_encode(x[a:a+160]); t=base+0.12+i*0.02
            records.append(PcapRecord(t,udp_ethernet_frame(device_ip,gateway_ip,4000,5000,build_rtp(payload,sequence=100+i,timestamp=i*160,ssrc=0x11111111,payload_type=8),ident=ident))); ident+=1
            records.append(PcapRecord(t+0.001,udp_ethernet_frame(gateway_ip,device_ip,5000,4000,build_rtp(payload,sequence=200+i,timestamp=i*160,ssrc=0x22222222,payload_type=8),ident=ident))); ident+=1
        for i in range(40):
            a=i*80; t=base+0.12+i*0.01
            records.append(PcapRecord(t,udp_ethernet_frame(device_ip,gateway_ip,41000,40000,x[a:a+80].astype('<i2').tobytes(),ident=ident))); ident+=1
            records.append(PcapRecord(t+0.0002,udp_ethernet_frame(device_ip,gateway_ip,41001,50000,x[a:a+80].astype('<i2').tobytes(),ident=ident))); ident+=1
        return MockCallScenario(build_pcap(records),f'{start_ms} SIP_INVITE LIVE_PROBE call={call_id}\n'.encode())

    def pretrigger(self, *, start_ms:int, end_ms:int, device_ip:str, gateway_ip:str) -> MockCallScenario:
        duration_ms=max(10,end_ms-start_ms); n=max(80,int(self.sample_rate*duration_ms/1000.0)); x=_normal_signal(n,self.sample_rate)
        records=[]; base=1_700_000_000.0+start_ms/1000.0; ident=1
        for i in range(0,n-79,80):
            t=base+(i/80)*0.01
            records.append(PcapRecord(t,udp_ethernet_frame(device_ip,gateway_ip,41000,40000,x[i:i+80].astype('<i2').tobytes(),ident=ident))); ident+=1
            records.append(PcapRecord(t+0.0002,udp_ethernet_frame(device_ip,gateway_ip,41001,50000,x[i:i+80].astype('<i2').tobytes(),ident=ident))); ident+=1
        return MockCallScenario(build_pcap(records),f'{start_ms} WATCHING\n{end_ms} PRETRIGGER_READY\n'.encode())
