"""Real-format PCM mirror + media analysis pipeline readiness tests.

Locks in the gap-closing verification that REAL PCM mirror streams (the verified
format: 160B / 8000Hz / 16-bit signed little-endian / 1ch on UDP 40000/50000) can
be decoded by the analysis engines. Before this, the real platform returned empty
pcaps from build_live_probe/build_call_capture, so real media never reached the
analyzers.

These tests build classic pcaps with the verified real-mirror format and assert:
  1. PcmIntelligenceEngine decodes them and detects DTMF (RX mirror on 40000);
  2. MediaIntelligenceEngine consumes a SIP+RTP+PCM-mirror pcap and produces
     RTP tracks + PCM sessions even without TShark (restricted fallback path).
"""
from __future__ import annotations

import socket
import struct
from pathlib import Path

import numpy as np

from app.analyzers.pcm import PcmIntelligenceEngine, PcmProfile, load_pcm_profile
from app.analyzers.pcm.profile import PcmTap
from app.analyzers.media.engine import MediaIntelligenceEngine
from app.analyzers.packet import TSharkAdapter


def _ether_ipv4_udp(src: str, dst: str, sp: int, dp: int, payload: bytes) -> bytes:
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
    udp = struct.pack('!HHHH', sp, dp, udp_len, 0)
    return eth + bytes(ip) + udp + payload


def _write_pcap(path: Path, packets: list[tuple[float, bytes]]) -> Path:
    with path.open('wb') as f:
        f.write(b'\xd4\xc3\xb2\xa1')
        f.write(struct.pack('<HHiiii', 2, 4, 0, 0, 262144, 1))
        for ts, data in packets:
            sec = int(ts)
            usec = int(round((ts - sec) * 1_000_000))
            if usec >= 1_000_000:
                sec += 1
                usec -= 1_000_000
            f.write(struct.pack('<IIII', sec, usec, len(data), len(data)))
            f.write(data)
    return path


def _tone(digit: str, fs=8000, on_ms=100, off_ms=100) -> np.ndarray:
    table = {'8': (852, 1336), '0': (941, 1336), '3': (697, 1477)}
    f1, f2 = table[digit]
    n = int(fs * on_ms / 1000)
    t = np.arange(n) / fs
    x = (3500 * np.sin(2 * np.pi * f1 * t) + 3500 * np.sin(2 * np.pi * f2 * t)).astype('<i2')
    z = np.zeros(int(fs * off_ms / 1000), dtype='<i2')
    return np.concatenate([x, z])


def _verified_profile() -> PcmProfile:
    # Mirrors profiles/pcm/ruijie_aim_diag_v1.yaml (the VERIFIED real-mirror format).
    return PcmProfile(
        id='ruijie_aim_diag_v1', sample_rate=8000, bit_depth=16, signed=True,
        endian='little', channels=1, packet_payload_bytes=160,
        expected_packet_interval_ms=10, session_gap_ms=100,
        taps=[PcmTap(name='pcm_rx', direction='RX', dst_port=40000),
              PcmTap(name='pcm_tx', direction='TX', dst_port=50000)],
    )


def test_real_format_pcm_mirror_decodes_dtmf(tmp_path):
    """RX mirror stream (UDP 40000) in the verified real format decodes to DTMF."""
    samples = np.concatenate([_tone('8'), _tone('8'), _tone('0'), _tone('3')])
    raw = samples.tobytes()
    packets = []
    for i in range(0, len(raw), 160):
        chunk = raw[i:i + 160]
        if len(chunk) != 160:
            continue
        packets.append((1000.0 + (i // 160) * 0.010,
                        _ether_ipv4_udp('192.168.3.200', '192.168.3.1', 6000, 40000, chunk)))
    pcap = _write_pcap(tmp_path / 'real_pcm_rx.pcap', packets)
    result = PcmIntelligenceEngine(_verified_profile()).analyze_pcap(pcap)
    assert result['status'] == 'SUCCESS'
    assert result['summary']['total_packets'] == 80
    session = result['streams'][0]['sessions'][0]
    assert session['analysis_availability'] == 'AVAILABLE'
    assert session['dtmf_sequences'][0]['digits'] == '8803'


def test_real_format_pcm_tx_mirror_decodes(tmp_path):
    """TX mirror stream (UDP 50000) in the verified real format decodes."""
    silence = np.zeros(80, dtype='<i2').tobytes()
    packets = [(ts, _ether_ipv4_udp('192.168.3.1', '192.168.3.200', 6000, 50000, silence))
               for ts in (1.00, 1.01, 1.02, 1.03, 1.04)]
    pcap = _write_pcap(tmp_path / 'real_pcm_tx.pcap', packets)
    result = PcmIntelligenceEngine(_verified_profile()).analyze_pcap(pcap)
    tx = next(s for s in result['streams'] if s['tap']['name'] == 'pcm_tx')
    assert tx['packet_count'] == 5
    assert tx['sessions'][0]['analysis_availability'] == 'AVAILABLE'


def _rtp_frame(seq: int, ts: int, payload: bytes) -> bytes:
    b1 = 0x80 | (8 & 0x7F)
    head = struct.pack('!BBHII', 0x80, b1, seq & 0xFFFF, ts & 0xFFFFFFFF, 0x11223344)
    return head + payload


def test_media_engine_consumes_sip_rtp_and_pcm_mirror(tmp_path):
    """MediaIntelligenceEngine digests a SIP + RTP + PCM-mirror pcap end to end.

    Without TShark the RTP layer is supplemented by the restricted fallback; the
    PCM mirror is decoded by PcmIntelligenceEngine. Both must surface in the result.
    """
    records: list[tuple[float, bytes]] = []
    sip = ('INVITE sip:100@192.168.3.1 SIP/2.0\r\nVia: SIP/2.0/UDP 192.168.3.200\r\n'
           'Content-Type: application/sdp\r\n\r\n'
           'v=0\r\no=- 0 0 IN IP4 192.168.3.200\r\nc=IN IP4 192.168.3.200\r\n'
           'm=audio 6000 RTP/AVP 8\r\na=rtpmap:8 PCMA/8000\r\n')
    records.append((100.0, _ether_ipv4_udp('192.168.3.200', '192.168.3.1', 5060, 5060, sip.encode())))
    t = np.arange(80) / 8000
    pcm = np.clip(0.3 * np.sin(2 * np.pi * 440 * t) * 32767, -32768, 32767).astype('<i2').tobytes()
    assert len(pcm) == 160
    for i in range(30):
        rtp = _rtp_frame(1000 + i, i * 160, pcm)
        records.append((100.5 + i * 0.02,
                        _ether_ipv4_udp('192.168.3.200', '192.168.3.1', 6000, 6000, rtp)))
        records.append((100.5 + i * 0.02,
                        _ether_ipv4_udp('192.168.3.200', '192.168.3.1', 6000, 40000, pcm)))
    records.append((102.0, _ether_ipv4_udp('192.168.3.200', '192.168.3.1', 5060, 5060,
                                           b'BYE sip:100@192.168.3.1 SIP/2.0\r\nVia: SIP/2.0/UDP 192.168.3.200\r\n\r\n')))
    pcap = _write_pcap(tmp_path / 'media_full.pcap', records)
    engine = MediaIntelligenceEngine(_verified_profile(), TSharkAdapter())
    result = engine.analyze_pcap(pcap, tmp_path / 'out')
    summ = result['summary'] or {}
    # RTP stream surfaced (via fallback since no TShark), one decoded track.
    assert summ.get('rtp_stream_count', 0) >= 1
    assert summ.get('decoded_rtp_track_count', 0) >= 1
    # PCM mirror decoded into a session.
    assert summ.get('pcm_session_count', 0) >= 1
    assert result['status'] in ('SUCCESS', 'PARTIAL_SUCCESS')


def test_real_platform_loads_verified_profile_matches_format(tmp_path):
    """The shipped VERIFIED profile still matches the format this pipeline assumes."""
    path = Path('/app/profiles/pcm/ruijie_aim_diag_v1.yaml')
    if not path.exists():
        path = Path(__file__).resolve().parents[2] / 'profiles' / 'pcm' / 'ruijie_aim_diag_v1.yaml'
    profile = load_pcm_profile(path)
    assert profile.can_decode is True
    assert profile.sample_rate == 8000
    assert profile.bit_depth == 16
    assert profile.endian == 'little'
    assert profile.channels == 1
    assert profile.packet_payload_bytes == 160
    ports = {tap.dst_port for tap in profile.taps}
    assert {40000, 50000} <= ports
