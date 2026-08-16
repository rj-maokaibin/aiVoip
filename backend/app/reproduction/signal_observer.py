from __future__ import annotations

"""Deterministic call/media observations derived from a frozen PCAP segment.

PCM mirrors prove that the local audio tap is alive, but they do not prove that a
SIP call exists (dial tone and digit collection can produce PCM before INVITE).
This observer therefore keeps Attempt, Call and Media signals separate:

* UDP/40000 and UDP/50000 -> PCM data-plane health only;
* SIP INVITE -> preferred Call binding;
* a progressing RTP-v2 flow -> reconstructable media/call fallback.

The implementation is deliberately small and deterministic so it can run in the
watch loop without invoking an LLM or a heavyweight analyzer.
"""

import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from app.analyzers.pcm.pcap_udp import PcapFormatError, iter_udp_datagrams


_CALL_ID = re.compile(br"(?im)^Call-ID\s*:\s*([^\r\n]+)")


@dataclass(frozen=True)
class CaptureSignalObservation:
    udp_packets: int = 0
    pcm_rx_packets: int = 0
    pcm_tx_packets: int = 0
    sip_invites: int = 0
    rtp_packets: int = 0
    call_id: str | None = None
    rtp_flow_ref: str | None = None
    call_binding_timestamp: float | None = None
    first_timestamp: float | None = None
    last_timestamp: float | None = None
    parse_error: str | None = None

    @property
    def pcm_stream_verified(self) -> bool:
        return self.pcm_rx_packets > 0 and self.pcm_tx_packets > 0

    @property
    def call_binding_event(self) -> str | None:
        if self.sip_invites > 0:
            return "SIP_INVITE"
        if self.rtp_packets >= 3:
            return "RTP_STREAM_START_FALLBACK"
        return None

    @property
    def external_call_ref(self) -> str | None:
        return self.call_id or self.rtp_flow_ref

    def as_dict(self) -> dict:
        return {
            "udp_packets": self.udp_packets,
            "pcm_rx_packets": self.pcm_rx_packets,
            "pcm_tx_packets": self.pcm_tx_packets,
            "pcm_stream_verified": self.pcm_stream_verified,
            "sip_invites": self.sip_invites,
            "rtp_packets": self.rtp_packets,
            "call_binding_event": self.call_binding_event,
            "external_call_ref": self.external_call_ref,
            "call_binding_timestamp": self.call_binding_timestamp,
            "first_timestamp": self.first_timestamp,
            "last_timestamp": self.last_timestamp,
            "parse_error": self.parse_error,
        }


def _looks_like_rtp(payload: bytes) -> bool:
    if len(payload) < 12 or payload[0] >> 6 != 2:
        return False
    # RTP payload types are seven bits. RTCP packet types occupy 192..223 in the
    # full second byte and are intentionally not treated as media binding here.
    return not 192 <= payload[1] <= 223


def observe_pcap_signals(path: str | Path) -> CaptureSignalObservation:
    udp_packets = pcm_rx = pcm_tx = sip_invites = 0
    call_id: str | None = None
    first_ts: float | None = None
    last_ts: float | None = None
    invite_timestamp: float | None = None
    rtp_flows: dict[tuple[str, int, str, int, int], list[tuple[int, int, float]]] = defaultdict(list)
    try:
        for datagram in iter_udp_datagrams(path):
            udp_packets += 1
            first_ts = datagram.timestamp if first_ts is None else min(first_ts, datagram.timestamp)
            last_ts = datagram.timestamp if last_ts is None else max(last_ts, datagram.timestamp)
            ports = {datagram.src_port, datagram.dst_port}
            if 40000 in ports:
                pcm_rx += 1
                continue
            if 50000 in ports:
                pcm_tx += 1
                continue
            first_line = datagram.payload.split(b"\r\n", 1)[0].strip().upper()
            if first_line.startswith(b"INVITE ") and first_line.endswith(b" SIP/2.0"):
                sip_invites += 1
                invite_timestamp = datagram.timestamp if invite_timestamp is None else min(invite_timestamp, datagram.timestamp)
                match = _CALL_ID.search(datagram.payload)
                if match and call_id is None:
                    call_id = match.group(1).decode("utf-8", errors="replace").strip()
                continue
            if 5060 not in ports and _looks_like_rtp(datagram.payload):
                ssrc = int.from_bytes(datagram.payload[8:12], "big")
                flow = (datagram.src_ip, datagram.src_port, datagram.dst_ip, datagram.dst_port, ssrc)
                sequence = int.from_bytes(datagram.payload[2:4], "big")
                timestamp = int.from_bytes(datagram.payload[4:8], "big")
                rtp_flows[flow].append((sequence, timestamp, datagram.timestamp))
    except (OSError, PcapFormatError, ValueError) as exc:
        return CaptureSignalObservation(parse_error=f"{type(exc).__name__}:{exc}")

    progressing: list[tuple[tuple[str, int, str, int, int], int, float]] = []
    for flow, packets in rtp_flows.items():
        forward = [((b[0] - a[0]) & 0xFFFF) for a, b in zip(packets, packets[1:])]
        timestamp_progress = any(a[1] != b[1] for a, b in zip(packets, packets[1:]))
        if len(packets) >= 3 and timestamp_progress and sum(1 for delta in forward if 0 < delta <= 1000) >= 2:
            progressing.append((flow, len(packets), min(packet[2] for packet in packets)))
    progressing.sort(key=lambda item: item[1], reverse=True)
    rtp_packets = sum(count for _, count, _ in progressing)
    rtp_flow_ref = None
    if progressing:
        src_ip, src_port, dst_ip, dst_port, ssrc = progressing[0][0]
        rtp_flow_ref = f"rtp:{src_ip}:{src_port}>{dst_ip}:{dst_port};ssrc={ssrc}"
    binding_timestamp = invite_timestamp or (progressing[0][2] if progressing else None)
    return CaptureSignalObservation(
        udp_packets=udp_packets,
        pcm_rx_packets=pcm_rx,
        pcm_tx_packets=pcm_tx,
        sip_invites=sip_invites,
        rtp_packets=rtp_packets,
        call_id=call_id,
        rtp_flow_ref=rtp_flow_ref,
        call_binding_timestamp=binding_timestamp,
        first_timestamp=first_ts,
        last_timestamp=last_ts,
    )


def binding_relative_ms(
    observation: CaptureSignalObservation,
    *,
    segment_start_ms: int,
    segment_end_ms: int,
) -> int:
    """Map the binding packet's PCAP time onto the session monotonic timeline.

    Segment start/end are collector-clock bounds.  A packet timestamp offset is
    clamped to those bounds so clock precision or capture startup latency cannot
    create an impossible anchor outside the segment.
    """
    if observation.call_binding_timestamp is None or observation.first_timestamp is None:
        return int(segment_end_ms)
    offset_ms = round((observation.call_binding_timestamp - observation.first_timestamp) * 1000)
    return max(int(segment_start_ms), min(int(segment_end_ms), int(segment_start_ms) + offset_ms))
