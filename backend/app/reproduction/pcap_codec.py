from __future__ import annotations

"""Deterministic classic-PCAP codec used only by the Phase-C Mock Platform.

The production collector will replace the writer with the EC-02 device tcpdump stream.
The reader side is deliberately narrow: Ethernet + IPv4 + UDP, SIP text and RTP v2.
It exists so Mock Platform E2E exercises the real PCAP/PCM/media analyzer pipeline
without requiring TShark or a physical DUT.
"""

import ipaddress
import re
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

from app.analyzers.packet.types import NormalizedPacket, RtpData, SdpData, SipData
from app.analyzers.pcm.pcap_udp import iter_udp_datagrams

_PCAP_HEADER = struct.pack('<IHHIIII', 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1)


def _ipv4(value: str) -> bytes:
    return ipaddress.ip_address(value).packed


def udp_ethernet_frame(src_ip: str, dst_ip: str, src_port: int, dst_port: int, payload: bytes, *, ident: int = 0) -> bytes:
    eth = b'\x02\x00\x00\x00\x00\x02' + b'\x02\x00\x00\x00\x00\x01' + struct.pack('!H', 0x0800)
    udp_len = 8 + len(payload)
    total_len = 20 + udp_len
    ip = struct.pack('!BBHHHBBH4s4s', 0x45, 0, total_len, ident & 0xFFFF, 0, 64, 17, 0, _ipv4(src_ip), _ipv4(dst_ip))
    udp = struct.pack('!HHHH', int(src_port), int(dst_port), udp_len, 0)
    return eth + ip + udp + payload


@dataclass(frozen=True)
class PcapRecord:
    timestamp: float
    frame: bytes


def build_pcap(records: Iterable[PcapRecord]) -> bytes:
    out = bytearray(_PCAP_HEADER)
    for record in sorted(records, key=lambda x: x.timestamp):
        sec = int(record.timestamp)
        usec = int(round((record.timestamp - sec) * 1_000_000))
        if usec >= 1_000_000:
            sec += 1; usec -= 1_000_000
        out.extend(struct.pack('<IIII', sec, usec, len(record.frame), len(record.frame)))
        out.extend(record.frame)
    return bytes(out)


def merge_classic_pcaps(paths: Iterable[str | Path], output: str | Path) -> Path:
    paths = [Path(p) for p in paths]
    output = Path(output); output.parent.mkdir(parents=True, exist_ok=True)
    if not paths:
        output.write_bytes(_PCAP_HEADER)
        return output
    header = None
    with output.open('wb') as dst:
        for idx, path in enumerate(paths):
            data = path.read_bytes()
            if len(data) < 24:
                raise ValueError(f'PCAP_SEGMENT_TRUNCATED:{path}')
            current = data[:24]
            if current[:4] != _PCAP_HEADER[:4] or current[20:24] != _PCAP_HEADER[20:24]:
                raise ValueError(f'PCAP_SEGMENT_FORMAT_MISMATCH:{path}')
            if header is None:
                header = current; dst.write(header)
            elif current != header:
                raise ValueError(f'PCAP_SEGMENT_HEADER_MISMATCH:{path}')
            dst.write(data[24:])
    return output


def build_rtp(payload: bytes, *, sequence: int, timestamp: int, ssrc: int, payload_type: int = 8, marker: bool = False) -> bytes:
    b0 = 0x80
    b1 = (0x80 if marker else 0) | (payload_type & 0x7F)
    return struct.pack('!BBHII', b0, b1, sequence & 0xFFFF, timestamp & 0xFFFFFFFF, ssrc & 0xFFFFFFFF) + payload


def _parse_rtp(payload: bytes) -> RtpData | None:
    if len(payload) < 12 or (payload[0] >> 6) != 2:
        return None
    cc = payload[0] & 0x0F
    extension = bool(payload[0] & 0x10)
    offset = 12 + cc * 4
    if len(payload) < offset:
        return None
    if extension:
        if len(payload) < offset + 4:
            return None
        ext_words = struct.unpack('!H', payload[offset + 2:offset + 4])[0]
        offset += 4 + ext_words * 4
        if len(payload) < offset:
            return None
    _, b1, seq, ts, ssrc = struct.unpack('!BBHII', payload[:12])
    media = payload[offset:]
    return RtpData(ssrc=ssrc, sequence=seq, timestamp=ts, payload_type=b1 & 0x7F, marker=bool(b1 & 0x80), payload_size=len(media), payload_hex=media.hex())


_SIP_START = re.compile(r'^(?:(REGISTER|INVITE|ACK|BYE|CANCEL|OPTIONS|UPDATE|INFO)\s+[^\s]+\s+SIP/2\.0|SIP/2\.0\s+(\d{3})\s*(.*))$', re.I)


def _parse_sip(payload: bytes) -> tuple[SipData, SdpData | None] | None:
    try:
        text = payload.decode('utf-8', errors='strict')
    except UnicodeDecodeError:
        return None
    head, sep, body = text.partition('\r\n\r\n')
    lines = head.split('\r\n')
    if not lines:
        return None
    m = _SIP_START.match(lines[0].strip())
    if not m:
        return None
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ':' not in line:
            continue
        k, v = line.split(':', 1); headers[k.strip().lower()] = v.strip()
    method = m.group(1).upper() if m.group(1) else None
    status = int(m.group(2)) if m.group(2) else None
    cseq_no = None; cseq_method = None
    if headers.get('cseq'):
        parts = headers['cseq'].split()
        if parts:
            try: cseq_no = int(parts[0])
            except ValueError: pass
        if len(parts) > 1: cseq_method = parts[1].upper()
    def tag(header: str) -> str | None:
        mt = re.search(r';tag=([^;>\s]+)', header or '', re.I)
        return mt.group(1) if mt else None
    sip = SipData(
        method=method, status_code=status, reason_phrase=(m.group(3).strip() if status else None),
        call_id=headers.get('call-id'), cseq=cseq_no, cseq_method=cseq_method,
        from_uri=headers.get('from'), to_uri=headers.get('to'), from_tag=tag(headers.get('from','')),
        to_tag=tag(headers.get('to','')), via_branch=headers.get('via'), contact=headers.get('contact'),
        content_type=headers.get('content-type'), raw_start_line=lines[0].strip(),
    )
    sdp = SdpData(raw=body) if sep and body and 'application/sdp' in (headers.get('content-type') or '').lower() else None
    return sip, sdp


class MockPcapAdapter:
    """TSharkAdapter-compatible deterministic parser for Mock Platform PCAPs."""
    def version(self) -> str:
        return 'mock-pcap-adapter/1.0.0'

    def iter_packets(self, pcap_path: str | Path) -> Iterator[NormalizedPacket]:
        for d in iter_udp_datagrams(pcap_path):
            parsed_sip = _parse_sip(d.payload) if 5060 in {d.src_port, d.dst_port} else None
            if parsed_sip:
                sip, sdp = parsed_sip
                yield NormalizedPacket(
                    frame_number=d.frame_number, timestamp=d.timestamp, src_ip=d.src_ip, dst_ip=d.dst_ip,
                    transport='UDP', src_port=d.src_port, dst_port=d.dst_port, protocols=['sip'] + (['sdp'] if sdp else []),
                    sip=sip, sdp=sdp,
                )
                continue
            if d.src_port in {4000,5000} or d.dst_port in {4000,5000}:
                rtp = _parse_rtp(d.payload)
                if rtp:
                    yield NormalizedPacket(
                        frame_number=d.frame_number, timestamp=d.timestamp, src_ip=d.src_ip, dst_ip=d.dst_ip,
                        transport='UDP', src_port=d.src_port, dst_port=d.dst_port, protocols=['rtp'], rtp=rtp,
                    )
