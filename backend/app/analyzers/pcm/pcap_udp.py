from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import socket
import struct
from typing import Iterator


@dataclass(slots=True)
class UdpDatagram:
    frame_number: int
    timestamp: float
    src_ip: str
    dst_ip: str
    src_port: int
    dst_port: int
    payload: bytes


class PcapFormatError(ValueError):
    pass


def iter_udp_datagrams(path: str | Path) -> Iterator[UdpDatagram]:
    """Small deterministic reader for private PCM UDP streams.

    Production SIP/RTP parsing remains TShark-based. This reader intentionally
    supports only the minimum required to extract opaque UDP payloads from
    Ethernet/VLAN + IPv4 PCAP files. Unsupported link/network formats fail
    explicitly instead of guessing.
    """
    path = Path(path)
    with path.open("rb") as fh:
        gh = fh.read(24)
        if len(gh) != 24:
            raise PcapFormatError("PCAP_GLOBAL_HEADER_TRUNCATED")
        magic = gh[:4]
        endian_map = {
            b"\xd4\xc3\xb2\xa1": ("<", 1_000_000.0),
            b"\xa1\xb2\xc3\xd4": (">", 1_000_000.0),
            b"\x4d\x3c\xb2\xa1": ("<", 1_000_000_000.0),
            b"\xa1\xb2\x3c\x4d": (">", 1_000_000_000.0),
        }
        if magic not in endian_map:
            raise PcapFormatError("UNSUPPORTED_PCAP_MAGIC")
        endian, ts_divisor = endian_map[magic]
        _, _, _, _, _, network = struct.unpack(endian + "HHiiii", gh[4:24])
        if network != 1:
            raise PcapFormatError(f"UNSUPPORTED_LINKTYPE:{network}")

        frame = 0
        while True:
            ph = fh.read(16)
            if not ph:
                break
            if len(ph) != 16:
                raise PcapFormatError("PCAP_PACKET_HEADER_TRUNCATED")
            ts_sec, ts_frac, incl_len, _ = struct.unpack(endian + "IIII", ph)
            data = fh.read(incl_len)
            if len(data) != incl_len:
                raise PcapFormatError("PCAP_PACKET_TRUNCATED")
            frame += 1
            packet = _parse_ethernet_ipv4_udp(data)
            if packet is None:
                continue
            src_ip, dst_ip, src_port, dst_port, payload = packet
            yield UdpDatagram(
                frame_number=frame,
                timestamp=ts_sec + ts_frac / ts_divisor,
                src_ip=src_ip,
                dst_ip=dst_ip,
                src_port=src_port,
                dst_port=dst_port,
                payload=payload,
            )


def _parse_ethernet_ipv4_udp(data: bytes):
    if len(data) < 14:
        return None
    ethertype = struct.unpack("!H", data[12:14])[0]
    offset = 14
    while ethertype in {0x8100, 0x88A8, 0x9100}:
        if len(data) < offset + 4:
            return None
        _, ethertype = struct.unpack("!HH", data[offset:offset + 4])
        offset += 4
    if ethertype != 0x0800 or len(data) < offset + 20:
        return None
    version_ihl = data[offset]
    if version_ihl >> 4 != 4:
        return None
    ihl = (version_ihl & 0x0F) * 4
    if ihl < 20 or len(data) < offset + ihl:
        return None
    total_len = struct.unpack("!H", data[offset + 2:offset + 4])[0]
    if data[offset + 9] != 17:
        return None
    src_ip = socket.inet_ntoa(data[offset + 12:offset + 16])
    dst_ip = socket.inet_ntoa(data[offset + 16:offset + 20])
    udp_offset = offset + ihl
    ip_end = min(len(data), offset + total_len)
    if len(data) < udp_offset + 8:
        return None
    src_port, dst_port, udp_len, _ = struct.unpack("!HHHH", data[udp_offset:udp_offset + 8])
    udp_end = min(ip_end, udp_offset + udp_len)
    payload = data[udp_offset + 8:udp_end]
    return src_ip, dst_ip, src_port, dst_port, payload
