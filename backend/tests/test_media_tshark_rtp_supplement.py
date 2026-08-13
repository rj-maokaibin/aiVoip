import socket
import struct
from pathlib import Path

from app.analyzers.media.engine import MediaIntelligenceEngine
from app.analyzers.pcm.profile import PcmProfile


class EmptyTShark:
    def iter_packets(self, path):
        return iter(())
    def version(self):
        return 'fake-tshark 1.0'


def _frame(seq: int, ts: int) -> bytes:
    payload = bytes([0xD5]) * 160
    rtp = bytes([0x80, 8]) + struct.pack('!HII', seq, ts, 0x12345678) + payload
    udp = struct.pack('!HHHH', 10000, 20000, 8 + len(rtp), 0) + rtp
    ip = bytearray(20); ip[0] = 0x45; struct.pack_into('!H', ip, 2, 20 + len(udp)); ip[8] = 64; ip[9] = 17
    ip[12:16] = socket.inet_aton('10.0.0.1'); ip[16:20] = socket.inet_aton('10.0.0.2')
    return b'\x00' * 12 + b'\x08\x00' + bytes(ip) + udp


def _write(path: Path):
    with path.open('wb') as f:
        f.write(b'\xd4\xc3\xb2\xa1' + struct.pack('<HHiiii', 2, 4, 0, 0, 65535, 1))
        for i in range(25):
            data = _frame(100 + i, i * 160)
            f.write(struct.pack('<IIII', 1, i * 20000, len(data), len(data)))
            f.write(data)


def test_media_supplements_rtp_when_tshark_does_not_decode_dynamic_ports(tmp_path):
    pcap = tmp_path / 'rtp.pcap'; _write(pcap)
    profile = PcmProfile('none', 8000, 16, True, 'little', 1, 160, 10, 100, [])
    result = MediaIntelligenceEngine(profile, EmptyTShark()).analyze_pcap(pcap, tmp_path / 'out')
    assert result['status'] == 'PARTIAL_SUCCESS'
    assert result['summary']['rtp_stream_count'] == 1
    assert result['packet']['source']['parser'] == 'tshark+restricted_rtp_fallback'
    assert result['packet']['availability']['sip'] == 'AVAILABLE'
