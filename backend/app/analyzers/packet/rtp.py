from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from statistics import mean, median
from typing import Iterable

from .sdp import STATIC_PAYLOADS
from .types import NormalizedPacket
from app.analyzers.profile import get_default_analyzer_profile


@dataclass(frozen=True, slots=True)
class RtpPtimeHint:
    timestamp: float
    ip: str
    port: int
    payload_types: tuple[int, ...]
    ptime_ms: float


@dataclass(slots=True)
class RtpEvent:
    type: str
    start_time: float
    severity: str
    details: dict


class RtpStreamAnalyzer:
    def __init__(self, payload_map: dict[int, tuple[str, int]] | None = None, ptime_hints: list[RtpPtimeHint] | None = None):
        self.payload_map = {**STATIC_PAYLOADS, **(payload_map or {})}
        self.ptime_hints = list(ptime_hints or [])
        self.profile = get_default_analyzer_profile()
        self.config = self.profile.section("rtp")

    def analyze(self, packets: Iterable[NormalizedPacket]) -> list[dict]:
        grouped: dict[tuple, list[NormalizedPacket]] = defaultdict(list)
        for packet in packets:
            if not packet.rtp or packet.rtp.sequence is None:
                continue
            key = (
                packet.src_ip,
                packet.src_port,
                packet.dst_ip,
                packet.dst_port,
                packet.rtp.ssrc,
            )
            grouped[key].append(packet)
        results = []
        for key, group in grouped.items():
            group.sort(key=lambda p: (p.timestamp, p.frame_number))
            results.append(self._analyze_stream(key, group))
        results.sort(key=lambda x: x["start_time"])
        return results

    def _analyze_stream(self, key: tuple, packets: list[NormalizedPacket]) -> dict:
        src_ip, src_port, dst_ip, dst_port, ssrc = key
        seq_exts: list[int] = []
        timestamp_exts: list[int | None] = []
        seen: set[int] = set()
        max_seen: int | None = None
        reference: int | None = None
        timestamp_reference: int | None = None
        duplicates = 0
        reordered = 0
        payload_changes = 0
        last_payload: int | None = None
        deltas_ms: list[float] = []
        arrival_prev: float | None = None
        unique_packets: dict[int, NormalizedPacket] = {}

        for packet in packets:
            seq = packet.rtp.sequence & 0xFFFF
            ext = _extend_mod(seq, reference, 1 << 16)
            reference = ext if reference is None else max(reference, ext)
            if ext in seen:
                duplicates += 1
            else:
                if max_seen is not None and ext < max_seen:
                    reordered += 1
                seen.add(ext)
                unique_packets[ext] = packet
                max_seen = ext if max_seen is None else max(max_seen, ext)
            seq_exts.append(ext)
            ts = packet.rtp.timestamp
            if ts is not None:
                ts_ext = _extend_mod(ts & 0xFFFFFFFF, timestamp_reference, 1 << 32)
                timestamp_reference = ts_ext if timestamp_reference is None else max(timestamp_reference, ts_ext)
            else:
                ts_ext = None
            timestamp_exts.append(ts_ext)
            if last_payload is not None and packet.rtp.payload_type != last_payload:
                payload_changes += 1
            last_payload = packet.rtp.payload_type
            if arrival_prev is not None:
                deltas_ms.append(max(0.0, (packet.timestamp - arrival_prev) * 1000.0))
            arrival_prev = packet.timestamp

        unique_sorted = sorted(unique_packets)
        first_seq = unique_sorted[0]
        last_seq = unique_sorted[-1]
        expected = last_seq - first_seq + 1
        lost = max(0, expected - len(unique_sorted))
        loss_rate = (lost / expected * 100.0) if expected else 0.0
        burst_events = _burst_events(unique_sorted, unique_packets, int(self.config["burst_high_lost_packets"]))

        payload_types = [p.rtp.payload_type for p in packets if p.rtp.payload_type is not None]
        dominant_pt = max(set(payload_types), key=payload_types.count) if payload_types else None
        mapping = self.payload_map.get(dominant_pt)
        if mapping is None:
            codec = f"PT{dominant_pt}" if dominant_pt is not None else "UNKNOWN"
            clock_rate = None
        else:
            codec, clock_rate = mapping
        # EC-04: SDP ptime is authoritative when a temporally valid media-endpoint hint
        # exists. Otherwise infer from RTP timestamps only when clock rate is known.
        # Unknown/dynamic PT without an SDP mapping remains UNAVAILABLE.
        sdp_ptime = self._sdp_ptime_hint(key, dominant_pt, packets[0].timestamp)
        if sdp_ptime is not None:
            ptime_ms = sdp_ptime
            ptime_source = "SDP"
        else:
            ptime_ms = _infer_ptime(unique_packets, clock_rate, float(self.config["ptime_min_ms"]), float(self.config["ptime_max_ms"])) if clock_rate else None
            ptime_source = "RTP_TIMESTAMP_INFERRED" if ptime_ms is not None else "UNAVAILABLE"

        for event in burst_events:
            event.details["ptime_ms"] = ptime_ms
            event.details["estimated_audio_loss_ms"] = round(event.details["lost_packets"] * ptime_ms, 3) if ptime_ms else None

        jitter_values = _rfc3550_jitter(packets, clock_rate, float(self.config["jitter_filter_divisor"])) if clock_rate else []
        gap_events = list(burst_events)
        events = list(gap_events)
        if deltas_ms and ptime_ms:
            high_threshold = max(ptime_ms * float(self.config["high_delta_multiplier"]), ptime_ms + float(self.config["high_delta_additive_ms"]))
            for idx, delta in enumerate(deltas_ms, start=1):
                if delta >= high_threshold:
                    events.append(RtpEvent(
                        type="HIGH_DELTA",
                        start_time=packets[idx].timestamp,
                        severity="MEDIUM",
                        details={"delta_ms": round(delta, 3), "expected_ptime_ms": round(ptime_ms, 3), "excess_delay_ms": round(max(0.0, delta-ptime_ms), 3)},
                    ))
        if payload_changes:
            events.append(RtpEvent(
                type="PAYLOAD_CHANGE",
                start_time=packets[0].timestamp,
                severity="MEDIUM",
                details={"change_count": payload_changes, "payload_types": sorted(set(payload_types))},
            ))

        high_delta_events = [e for e in events if e.type == "HIGH_DELTA"]
        jitter_ms = [v * 1000.0 / clock_rate for v in jitter_values] if clock_rate else []
        return {
            "stream_id": _stream_id(key),
            "src_ip": src_ip,
            "src_port": src_port,
            "dst_ip": dst_ip,
            "dst_port": dst_port,
            "ssrc": ssrc,
            "packet_count": len(packets),
            "unique_packet_count": len(unique_sorted),
            "duration_seconds": round(packets[-1].timestamp - packets[0].timestamp, 6),
            "start_time": packets[0].timestamp,
            "end_time": packets[-1].timestamp,
            "first_sequence_ext": first_seq,
            "last_sequence_ext": last_seq,
            "expected_packets": expected,
            "lost_packets": lost,
            "loss_rate_percent": round(loss_rate, 6),
            "duplicate_packets": duplicates,
            "out_of_order_packets": reordered,
            "max_consecutive_loss": max((e.details["lost_packets"] for e in gap_events), default=0),
            "burst_loss_count": len(gap_events),
            "avg_delta_ms": round(mean(deltas_ms), 6) if deltas_ms else None,
            "p95_delta_ms": round(_percentile(deltas_ms, 0.95), 6) if deltas_ms else None,
            "max_delta_ms": round(max(deltas_ms), 6) if deltas_ms else None,
            "high_delta_count": len(high_delta_events),
            "max_excess_delay_ms": round(max((e.details.get("excess_delay_ms",0.0) for e in high_delta_events), default=0.0), 6),
            "avg_jitter_ms": round(mean(jitter_ms), 6) if jitter_ms else None,
            "p95_jitter_ms": round(_percentile(jitter_ms, 0.95), 6) if jitter_ms else None,
            "max_jitter_ms": round(max(jitter_ms), 6) if jitter_ms else None,
            "payload_type": dominant_pt,
            "payload_types": sorted(set(payload_types)),
            "payload_change_count": payload_changes,
            "codec": codec,
            "clock_rate": clock_rate,
            "ptime_ms": round(ptime_ms, 6) if ptime_ms else None,
            "ptime_source": ptime_source,
            "availability": {
                "codec_mapping": "AVAILABLE" if mapping is not None else "UNAVAILABLE",
                "clock_rate": "AVAILABLE" if clock_rate else "UNAVAILABLE",
                "ptime": "AVAILABLE" if ptime_ms is not None else "UNAVAILABLE",
                "rfc3550_jitter": "AVAILABLE" if jitter_ms else "UNAVAILABLE",
                "estimated_audio_loss": "AVAILABLE" if ptime_ms is not None else "UNAVAILABLE",
            },
            "analyzer_profile": self.profile.metadata(),
            "events": [asdict(e) for e in sorted(events, key=lambda e: e.start_time)],
        }

    def _sdp_ptime_hint(self, key: tuple, payload_type: int | None, stream_start: float) -> float | None:
        if payload_type is None:
            return None
        src_ip, src_port, dst_ip, dst_port, _ = key
        matches = [
            hint for hint in self.ptime_hints
            if hint.timestamp <= stream_start
            and payload_type in hint.payload_types
            and ((hint.ip, hint.port) == (src_ip, src_port) or (hint.ip, hint.port) == (dst_ip, dst_port))
        ]
        if not matches:
            return None
        matches.sort(key=lambda hint: hint.timestamp, reverse=True)
        value = float(matches[0].ptime_ms)
        if float(self.config["ptime_min_ms"]) <= value <= float(self.config["ptime_max_ms"]):
            return value
        return None



def _extend_mod(value: int, reference: int | None, modulus: int) -> int:
    if reference is None:
        return value
    base = reference - (reference % modulus)
    candidates = (base + value, base + value - modulus, base + value + modulus)
    return min(candidates, key=lambda x: abs(x - reference))


def _burst_events(unique_sorted: list[int], packet_by_seq: dict[int, NormalizedPacket], high_loss_threshold: int) -> list[RtpEvent]:
    events: list[RtpEvent] = []
    for prev, current in zip(unique_sorted, unique_sorted[1:]):
        gap = current - prev - 1
        if gap > 0:
            packet = packet_by_seq[current]
            events.append(RtpEvent(
                type="BURST_LOSS" if gap > 1 else "PACKET_LOSS",
                start_time=packet.timestamp,
                severity="HIGH" if gap >= high_loss_threshold else "MEDIUM",
                details={
                    "previous_sequence_ext": prev,
                    "next_sequence_ext": current,
                    "lost_packets": gap,
                },
            ))
    return events


def _infer_ptime(packet_by_seq: dict[int, NormalizedPacket], clock_rate: int, ptime_min_ms: float, ptime_max_ms: float) -> float | None:
    samples: list[float] = []
    seqs = sorted(packet_by_seq)
    timestamp_reference: int | None = None
    ext_ts: dict[int, int] = {}
    for seq in seqs:
        ts = packet_by_seq[seq].rtp.timestamp
        if ts is None:
            continue
        ext = _extend_mod(ts & 0xFFFFFFFF, timestamp_reference, 1 << 32)
        timestamp_reference = ext if timestamp_reference is None else max(timestamp_reference, ext)
        ext_ts[seq] = ext
    for a, b in zip(seqs, seqs[1:]):
        if a not in ext_ts or b not in ext_ts:
            continue
        seq_diff = b - a
        ts_diff = ext_ts[b] - ext_ts[a]
        if seq_diff > 0 and ts_diff > 0:
            samples.append((ts_diff / seq_diff) * 1000.0 / clock_rate)
    valid = [x for x in samples if ptime_min_ms <= x <= ptime_max_ms]
    return median(valid) if valid else None


def _rfc3550_jitter(packets: list[NormalizedPacket], clock_rate: int, filter_divisor: float) -> list[float]:
    jitter = 0.0
    prev_transit: float | None = None
    timestamp_reference: int | None = None
    values: list[float] = []
    for packet in packets:
        if packet.rtp.timestamp is None:
            continue
        ts_ext = _extend_mod(packet.rtp.timestamp & 0xFFFFFFFF, timestamp_reference, 1 << 32)
        timestamp_reference = ts_ext if timestamp_reference is None else max(timestamp_reference, ts_ext)
        transit = packet.timestamp * clock_rate - ts_ext
        if prev_transit is not None:
            d = abs(transit - prev_transit)
            jitter += (d - jitter) / filter_divisor
            values.append(jitter)
        prev_transit = transit
    return values


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered=sorted(values)
    pos=(len(ordered)-1)*q
    lo=int(pos); hi=min(lo+1,len(ordered)-1)
    frac=pos-lo
    return ordered[lo]*(1-frac)+ordered[hi]*frac

def _stream_id(key: tuple) -> str:
    src_ip, src_port, dst_ip, dst_port, ssrc = key
    return f"{src_ip}:{src_port}>{dst_ip}:{dst_port}/ssrc={ssrc}"
