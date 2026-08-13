from __future__ import annotations

from dataclasses import asdict
from typing import Iterable

from .types import NormalizedPacket


_PACKET_TYPES = {
    "200": "SR",
    "201": "RR",
    "202": "SDES",
    "203": "BYE",
    "204": "APP",
    "205": "RTPFB",
    "206": "PSFB",
    "SR": "SR",
    "RR": "RR",
    "SDES": "SDES",
    "BYE": "BYE",
}


def _packet_type(value: str | None) -> str:
    if value is None:
        return "UNKNOWN"
    text = str(value).strip().upper()
    return _PACKET_TYPES.get(text, text or "UNKNOWN")


def _ntp_middle_32_from_unix(timestamp: float) -> int | None:
    # PCAP absolute wall-clock is required. Relative/synthetic timestamps cannot be used
    # to derive RFC3550 RTT and are therefore left unavailable.
    if timestamp < 946684800.0:  # 2000-01-01 UTC; intentionally conservative sanity gate.
        return None
    ntp = timestamp + 2208988800.0
    seconds = int(ntp) & 0xFFFFFFFF
    fraction = int((ntp - int(ntp)) * (1 << 32)) & 0xFFFFFFFF
    return ((seconds & 0xFFFF) << 16) | (fraction >> 16)


def _rtt_seconds(packet: NormalizedPacket) -> float | None:
    if packet.rtcp is None or packet.rtcp.lsr is None or packet.rtcp.dlsr is None:
        return None
    arrival = _ntp_middle_32_from_unix(float(packet.timestamp))
    if arrival is None:
        return None
    # RFC3550 A - LSR - DLSR in units of 1/65536 seconds. Use modular arithmetic because
    # the middle-32 NTP field wraps.
    diff = (arrival - int(packet.rtcp.lsr) - int(packet.rtcp.dlsr)) & 0xFFFFFFFF
    value = diff / 65536.0
    # Reject implausible values rather than manufacturing an RTT from malformed reports.
    return value if 0.0 <= value <= 60.0 else None


def analyze_rtcp(packets: Iterable[NormalizedPacket]) -> list[dict]:
    """Normalize RTCP SR/RR/SDES/BYE reports without hiding raw report fields.

    RTT is emitted only when absolute PCAP wall-clock plus LSR/DLSR make RFC3550 A-LSR-DLSR
    computable. Otherwise its availability is explicitly UNAVAILABLE.
    """
    out = []
    for packet in packets:
        if not packet.rtcp:
            continue
        raw = asdict(packet.rtcp)
        rtt = _rtt_seconds(packet)
        report_type = _packet_type(packet.rtcp.packet_type)
        out.append({
            "frame_number": packet.frame_number,
            "timestamp": packet.timestamp,
            "src_ip": packet.src_ip,
            "dst_ip": packet.dst_ip,
            "report_type": report_type,
            **raw,
            "rtt_seconds": round(rtt, 6) if rtt is not None else None,
            "availability": {
                "fraction_lost": "AVAILABLE" if packet.rtcp.fraction_lost is not None else "UNAVAILABLE",
                "cumulative_lost": "AVAILABLE" if packet.rtcp.cumulative_lost is not None else "UNAVAILABLE",
                "jitter": "AVAILABLE" if packet.rtcp.jitter is not None else "UNAVAILABLE",
                "rtt": "AVAILABLE" if rtt is not None else "UNAVAILABLE",
            },
        })
    return out
