#!/usr/bin/env python3
from __future__ import annotations

import argparse
import socket
import struct
from pathlib import Path

import numpy as np

from app.analyzers.audio.g711 import decode_alaw


def _ether_ipv4_udp(src: str, dst: str, sport: int, dport: int, payload: bytes) -> bytes:
    eth = b'\x00' * 12 + struct.pack('!H', 0x0800)
    udp_len = 8 + len(payload)
    total = 20 + udp_len
    ip = bytearray(20)
    ip[0] = 0x45
    struct.pack_into('!H', ip, 2, total)
    ip[8] = 64
    ip[9] = 17
    ip[12:16] = socket.inet_aton(src)
    ip[16:20] = socket.inet_aton(dst)
    udp = struct.pack('!HHHH', sport, dport, udp_len, 0)
    return eth + bytes(ip) + udp + payload


def _pcap_write(path: Path, packets: list[tuple[float, bytes]]) -> None:
    with path.open('wb') as f:
        f.write(b'\xd4\xc3\xb2\xa1')
        f.write(struct.pack('<HHiiii', 2, 4, 0, 0, 262144, 1))
        for ts, data in sorted(packets, key=lambda x: x[0]):
            sec = int(ts)
            usec = int(round((ts - sec) * 1_000_000))
            f.write(struct.pack('<IIII', sec, usec, len(data), len(data)))
            f.write(data)


def _sip_request(method: str, uri: str, call_id: str, cseq: int, body: str = '', content_type: str | None = None) -> bytes:
    body_b = body.encode()
    headers = [
        f'{method} {uri} SIP/2.0',
        'Via: SIP/2.0/UDP 10.0.0.1:5060;branch=z9hG4bK-e2e-1',
        'From: <sip:1001@pbx>;tag=e2e-a',
        'To: <sip:1002@pbx>',
        f'Call-ID: {call_id}',
        f'CSeq: {cseq} {method}',
        'Contact: <sip:1001@10.0.0.1:5060>',
        'Max-Forwards: 70',
    ]
    if content_type:
        headers.append(f'Content-Type: {content_type}')
    headers.append(f'Content-Length: {len(body_b)}')
    return ('\r\n'.join(headers) + '\r\n\r\n').encode() + body_b


def _sip_response(code: int, reason: str, call_id: str, cseq: int, method: str, body: str = '', content_type: str | None = None) -> bytes:
    body_b = body.encode()
    headers = [
        f'SIP/2.0 {code} {reason}',
        'Via: SIP/2.0/UDP 10.0.0.1:5060;branch=z9hG4bK-e2e-1',
        'From: <sip:1001@pbx>;tag=e2e-a',
        'To: <sip:1002@pbx>;tag=e2e-b',
        f'Call-ID: {call_id}',
        f'CSeq: {cseq} {method}',
        'Contact: <sip:1002@10.0.0.2:5060>',
    ]
    if content_type:
        headers.append(f'Content-Type: {content_type}')
    headers.append(f'Content-Length: {len(body_b)}')
    return ('\r\n'.join(headers) + '\r\n\r\n').encode() + body_b


def _rtp(seq: int, timestamp: int, ssrc: int, payload: bytes, pt: int = 8) -> bytes:
    return bytes([0x80, pt & 0x7F]) + struct.pack('!HII', seq & 0xFFFF, timestamp & 0xFFFFFFFF, ssrc) + payload


def _alaw_encode_nearest(samples: np.ndarray) -> bytes:
    # Deterministic inverse LUT using the decoder implemented by the project itself.  This is
    # only a test-fixture encoder, not a production codec implementation.
    decoded = np.frombuffer(decode_alaw(bytes(range(256))), dtype='<i2').astype(np.int32)
    order = np.argsort(decoded)
    vals = decoded[order]
    x = np.asarray(samples, dtype=np.int32)
    idx = np.searchsorted(vals, x)
    idx = np.clip(idx, 1, len(vals) - 1)
    left = vals[idx - 1]
    right = vals[idx]
    pick_right = np.abs(right - x) < np.abs(x - left)
    nearest = np.where(pick_right, idx, idx - 1)
    codes = order[nearest].astype(np.uint8)
    return codes.tobytes()


def build_periodic_fixture(path: Path, seconds: float = 3.0) -> dict:
    sr = 8000
    n = int(sr * seconds)
    t = np.arange(n, dtype=np.float64) / sr
    # Deliberately weak/no 50-Hz fundamental.  Odd 50-Hz harmonics produce the same 20-ms
    # repetition / 10-ms inversion pattern that the APF1250 field case exercises.
    freqs = np.arange(150, 951, 100)
    amps = np.linspace(1150.0, 500.0, len(freqs))
    periodic = sum(a * np.sin(2 * np.pi * f * t) for f, a in zip(freqs, amps))
    periodic = np.clip(periodic, -10000, 10000).astype('<i2')
    rng = np.random.default_rng(20260812)
    downstream = np.clip(rng.normal(0, 950, n), -7000, 7000).astype('<i2')

    device = '10.0.0.1'; pbx = '10.0.0.2'; call_id = 'e2e-periodic@voip-ai'
    offer = (
        'v=0\r\n'
        'o=- 1 1 IN IP4 10.0.0.1\r\n'
        's=-\r\n'
        'c=IN IP4 10.0.0.1\r\n'
        't=0 0\r\n'
        'm=audio 4000 RTP/AVP 8\r\n'
        'a=rtpmap:8 PCMA/8000\r\n'
        'a=ptime:20\r\n'
        'a=sendrecv\r\n'
    )
    answer = (
        'v=0\r\n'
        'o=- 2 2 IN IP4 10.0.0.2\r\n'
        's=-\r\n'
        'c=IN IP4 10.0.0.2\r\n'
        't=0 0\r\n'
        'm=audio 5000 RTP/AVP 8\r\n'
        'a=rtpmap:8 PCMA/8000\r\n'
        'a=ptime:20\r\n'
        'a=sendrecv\r\n'
    )
    packets: list[tuple[float, bytes]] = []
    packets.append((1.000, _ether_ipv4_udp(device, pbx, 5060, 5060, _sip_request('INVITE', 'sip:1002@pbx', call_id, 1, offer, 'application/sdp'))))
    packets.append((1.025, _ether_ipv4_udp(pbx, device, 5060, 5060, _sip_response(100, 'Trying', call_id, 1, 'INVITE'))))
    packets.append((1.050, _ether_ipv4_udp(pbx, device, 5060, 5060, _sip_response(200, 'OK', call_id, 1, 'INVITE', answer, 'application/sdp'))))
    packets.append((1.060, _ether_ipv4_udp(device, pbx, 5060, 5060, _sip_request('ACK', 'sip:1002@pbx', call_id, 1))))

    media_start = 1.100
    pcm_raw = periodic.tobytes()
    # pcm_rx: 80 signed 16-bit samples every 10 ms => 160 bytes.
    for i in range(0, len(pcm_raw), 160):
        chunk = pcm_raw[i:i+160]
        if len(chunk) < 160:
            break
        ts = media_start + (i // 160) * 0.010
        packets.append((ts, _ether_ipv4_udp(device, pbx, 32984, 40000, chunk)))

    # RTP uses 160 samples / 20 ms.  Upstream carries the same signal; downstream is a control.
    seq_up = 1000; seq_down = 4000; rtp_ts = 0
    for sample_off in range(0, n - 159, 160):
        when = media_start + (sample_off // 160) * 0.020 + 0.028
        up_payload = _alaw_encode_nearest(periodic[sample_off:sample_off+160])
        down_payload = _alaw_encode_nearest(downstream[sample_off:sample_off+160])
        packets.append((when, _ether_ipv4_udp(device, pbx, 4000, 5000, _rtp(seq_up, rtp_ts, 0x11111111, up_payload))))
        packets.append((when + 0.003, _ether_ipv4_udp(pbx, device, 5000, 4000, _rtp(seq_down, rtp_ts, 0x22222222, down_payload))))
        seq_up += 1; seq_down += 1; rtp_ts += 160

    bye_t = media_start + seconds + 0.10
    packets.append((bye_t, _ether_ipv4_udp(device, pbx, 5060, 5060, _sip_request('BYE', 'sip:1002@pbx', call_id, 2))))
    packets.append((bye_t + 0.02, _ether_ipv4_udp(pbx, device, 5060, 5060, _sip_response(200, 'OK', call_id, 2, 'BYE'))))
    _pcap_write(path, packets)
    return {
        'path': str(path), 'packet_count': len(packets), 'duration_seconds': seconds,
        'expected': {'hypothesis': 'LOCAL_CAPTURE_PERIODIC_INTERFERENCE', 'pcm_port': 40000, 'codec': 'PCMA'},
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('output', type=Path)
    ap.add_argument('--seconds', type=float, default=3.0)
    args = ap.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    info = build_periodic_fixture(args.output, args.seconds)
    print(info)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
