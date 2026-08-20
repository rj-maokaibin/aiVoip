from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from statistics import mean, median
from typing import Iterable

from .sdp import STATIC_PAYLOADS
from .types import NormalizedPacket
from app.analyzers.profile import get_default_analyzer_profile


HIGH_DELTA_SEMANTICS_VERSION = "rtp-high-delta-semantics-v1"


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
            key = (packet.src_ip, packet.src_port, packet.dst_ip, packet.dst_port, packet.rtp.ssrc)
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
        payload_change_events: list[dict] = []
        last_payload: int | None = None
        last_payload_packet: NormalizedPacket | None = None
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
                payload_change_events.append({
                    "previous_payload_type": last_payload,
                    "new_payload_type": packet.rtp.payload_type,
                    "previous_frame_number": last_payload_packet.frame_number if last_payload_packet else None,
                    "current_frame_number": packet.frame_number,
                    "previous_timestamp": last_payload_packet.timestamp if last_payload_packet else None,
                    "current_timestamp": packet.timestamp,
                })
            last_payload = packet.rtp.payload_type
            last_payload_packet = packet
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
        events = list(burst_events)
        if deltas_ms and ptime_ms:
            high_threshold = max(ptime_ms * float(self.config["high_delta_multiplier"]), ptime_ms + float(self.config["high_delta_additive_ms"]))
            for idx, delta in enumerate(deltas_ms, start=1):
                if delta >= high_threshold:
                    previous_packet = packets[idx - 1]
                    current_packet = packets[idx]
                    semantics = _high_delta_semantics(
                        packets=packets,
                        current_index=idx,
                        previous_packet=previous_packet,
                        current_packet=current_packet,
                        delta_ms=delta,
                        ptime_ms=ptime_ms,
                        threshold_ms=high_threshold,
                        clock_rate=clock_rate,
                    )
                    events.append(RtpEvent(
                        type="HIGH_DELTA",
                        start_time=current_packet.timestamp,
                        severity="MEDIUM",
                        details={
                            "delta_ms": round(delta, 3),
                            "expected_ptime_ms": round(ptime_ms, 3),
                            "threshold_ms": round(high_threshold, 3),
                            "excess_delay_ms": round(max(0.0, delta - ptime_ms), 3),
                            "delta_ratio_to_ptime": round(delta / ptime_ms, 3) if ptime_ms else None,
                            "previous_frame_number": previous_packet.frame_number,
                            "current_frame_number": current_packet.frame_number,
                            "previous_timestamp": previous_packet.timestamp,
                            "current_timestamp": current_packet.timestamp,
                            "previous_sequence": previous_packet.rtp.sequence,
                            "current_sequence": current_packet.rtp.sequence,
                            **semantics,
                        },
                    ))
        if payload_changes:
            events.append(RtpEvent(
                type="PAYLOAD_CHANGE",
                start_time=payload_change_events[0]["current_timestamp"] if payload_change_events else packets[0].timestamp,
                severity="MEDIUM",
                details={
                    "change_count": payload_changes,
                    "payload_types": sorted(set(payload_types)),
                    "changes": payload_change_events,
                    "frame_numbers": [x["current_frame_number"] for x in payload_change_events],
                },
            ))

        high_delta_events = [e for e in events if e.type == "HIGH_DELTA"]
        jitter_ms = [v * 1000.0 / clock_rate for v in jitter_values] if clock_rate else []
        avg_jitter = round(mean(jitter_ms), 6) if jitter_ms else None
        p95_jitter = round(_percentile(jitter_ms, 0.95), 6) if jitter_ms else None
        max_jitter = round(max(jitter_ms), 6) if jitter_ms else None
        loss_rate_value = round(loss_rate, 6)
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
            "first_frame_number": packets[0].frame_number,
            "last_frame_number": packets[-1].frame_number,
            "first_sequence_ext": first_seq,
            "last_sequence_ext": last_seq,
            "expected_packets": expected,
            "lost_packets": lost,
            "loss_rate_percent": loss_rate_value,
            "loss_rate": loss_rate_value,
            "duplicate_packets": duplicates,
            "out_of_order_packets": reordered,
            "max_consecutive_loss": max((e.details["lost_packets"] for e in burst_events), default=0),
            "burst_loss_count": len(burst_events),
            "avg_delta_ms": round(mean(deltas_ms), 6) if deltas_ms else None,
            "p95_delta_ms": round(_percentile(deltas_ms, 0.95), 6) if deltas_ms else None,
            "max_delta_ms": round(max(deltas_ms), 6) if deltas_ms else None,
            "high_delta_count": len(high_delta_events),
            "high_delta_without_sequence_loss_count": sum(1 for e in high_delta_events if e.details.get("sequence_continuous") is True),
            "high_delta_catch_up_count": sum(1 for e in high_delta_events if (e.details.get("catch_up") or {}).get("status") in {"PARTIAL", "FULL"}),
            "max_excess_delay_ms": round(max((e.details.get("excess_delay_ms", 0.0) for e in high_delta_events), default=0.0), 6),
            "avg_jitter_ms": avg_jitter,
            "p95_jitter_ms": p95_jitter,
            "max_jitter_ms": max_jitter,
            "avg_rfc3550_jitter_ms": avg_jitter,
            "p95_rfc3550_jitter_ms": p95_jitter,
            "max_rfc3550_jitter_ms": max_jitter,
            "payload_type": dominant_pt,
            "payload_types": sorted(set(payload_types)),
            "payload_change_count": payload_changes,
            "codec": codec,
            "clock_rate": clock_rate,
            "ptime_ms": round(ptime_ms, 6) if ptime_ms else None,
            "ptime_source": ptime_source,
            "high_delta_semantics_version": HIGH_DELTA_SEMANTICS_VERSION,
            "availability": {
                "codec_mapping": "AVAILABLE" if mapping is not None else "UNAVAILABLE",
                "clock_rate": "AVAILABLE" if clock_rate else "UNAVAILABLE",
                "ptime": "AVAILABLE" if ptime_ms is not None else "UNAVAILABLE",
                "rfc3550_jitter": "AVAILABLE" if jitter_ms else "UNAVAILABLE",
                "estimated_audio_loss": "AVAILABLE" if ptime_ms is not None else "UNAVAILABLE",
                "frame_level_evidence": "AVAILABLE",
                "high_delta_semantics": "AVAILABLE" if ptime_ms is not None else "UNAVAILABLE",
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


def _high_delta_semantics(*, packets: list[NormalizedPacket], current_index: int,
                          previous_packet: NormalizedPacket, current_packet: NormalizedPacket,
                          delta_ms: float, ptime_ms: float, threshold_ms: float,
                          clock_rate: int | None) -> dict:
    prev_seq = previous_packet.rtp.sequence if previous_packet.rtp else None
    curr_seq = current_packet.rtp.sequence if current_packet.rtp else None
    seq_step = ((int(curr_seq) - int(prev_seq)) & 0xFFFF) if prev_seq is not None and curr_seq is not None else None
    sequence_continuous = seq_step == 1 if seq_step is not None else None
    sequence_gap_packets = max(0, seq_step - 1) if seq_step is not None and seq_step > 0 else None

    prev_rtp_ts = previous_packet.rtp.timestamp if previous_packet.rtp else None
    curr_rtp_ts = current_packet.rtp.timestamp if current_packet.rtp else None
    rtp_timestamp_step = ((int(curr_rtp_ts) - int(prev_rtp_ts)) & 0xFFFFFFFF) if prev_rtp_ts is not None and curr_rtp_ts is not None else None
    expected_rtp_timestamp_step = round(clock_rate * ptime_ms / 1000.0) if clock_rate else None
    if rtp_timestamp_step is not None and expected_rtp_timestamp_step:
        tolerance = max(1, round(expected_rtp_timestamp_step * 0.05))
        rtp_timestamp_continuous = abs(rtp_timestamp_step - expected_rtp_timestamp_step) <= tolerance
    else:
        rtp_timestamp_continuous = None

    if sequence_continuous is True and rtp_timestamp_continuous is not False:
        classification = "INTERARRIVAL_STALL_WITHOUT_RTP_GAP"
        loss_semantics = "NO_SEQUENCE_LOSS_AT_EVENT_BOUNDARY"
    elif sequence_continuous is False and seq_step is not None and seq_step > 1:
        classification = "INTERARRIVAL_STALL_WITH_SEQUENCE_GAP"
        loss_semantics = "SEQUENCE_GAP_PRESENT_AT_EVENT_BOUNDARY"
    elif sequence_continuous is True:
        classification = "INTERARRIVAL_STALL_WITH_MEDIA_TIMESTAMP_GAP"
        loss_semantics = "NO_SEQUENCE_LOSS_BUT_MEDIA_TIMESTAMP_STEP_ABNORMAL"
    else:
        classification = "INTERARRIVAL_STALL_BOUNDARY_UNCERTAIN"
        loss_semantics = "BOUNDARY_SEQUENCE_SEMANTICS_UNAVAILABLE"

    excess = max(0.0, delta_ms - ptime_ms)
    catch_up = _catch_up_after_high_delta(packets, current_index, ptime_ms, excess)
    return {
        "semantic_version": HIGH_DELTA_SEMANTICS_VERSION,
        "classification": classification,
        "loss_semantics": loss_semantics,
        "sequence_step": seq_step,
        "sequence_continuous": sequence_continuous,
        "sequence_gap_packets": sequence_gap_packets,
        "rtp_timestamp_step": rtp_timestamp_step,
        "expected_rtp_timestamp_step": expected_rtp_timestamp_step,
        "rtp_timestamp_continuous": rtp_timestamp_continuous,
        "catch_up": catch_up,
        "semantic_boundary": (
            "HIGH_DELTA 证明该 RTP Stream 的相邻包到达/发送间隔显著大于 ptime；"
            "只有 Sequence Gap 才能在该边界支持 Packet Loss。单独 HIGH_DELTA 不等同于丢包。"
        ),
        "threshold_context": {
            "threshold_ms": round(threshold_ms, 3),
            "expected_ptime_ms": round(ptime_ms, 3),
            "actual_delta_ms": round(delta_ms, 3),
        },
    }


def _catch_up_after_high_delta(packets: list[NormalizedPacket], current_index: int,
                               ptime_ms: float, excess_delay_ms: float,
                               max_following_packets: int = 8) -> dict:
    deltas: list[float] = []
    recovered_ms = 0.0
    accelerated = 0
    end = min(len(packets), current_index + 1 + max_following_packets)
    for idx in range(current_index + 1, end):
        delta = max(0.0, (packets[idx].timestamp - packets[idx - 1].timestamp) * 1000.0)
        deltas.append(delta)
        if delta < ptime_ms:
            recovered_ms += ptime_ms - delta
        if delta <= ptime_ms * 0.75:
            accelerated += 1
        if excess_delay_ms > 0 and recovered_ms >= excess_delay_ms * 0.8:
            break

    if excess_delay_ms <= 0 or not deltas:
        status = "NONE"
    elif recovered_ms >= excess_delay_ms * 0.8:
        status = "FULL"
    elif recovered_ms >= max(1.0, excess_delay_ms * 0.1):
        status = "PARTIAL"
    else:
        status = "NONE"
    return {
        "status": status,
        "observed": status in {"PARTIAL", "FULL"},
        "following_packet_count": len(deltas),
        "accelerated_interval_count": accelerated,
        "deltas_ms": [round(x, 3) for x in deltas],
        "min_delta_ms": round(min(deltas), 3) if deltas else None,
        "recovered_delay_ms": round(recovered_ms, 3),
        "excess_delay_ms": round(excess_delay_ms, 3),
        "recovery_ratio": round(min(1.0, recovered_ms / excess_delay_ms), 3) if excess_delay_ms > 0 else 0.0,
        "definition": "catch-up 表示 HIGH_DELTA 后若干 RTP 到达间隔短于 ptime，回收了部分/大部分先前停顿时间；不代表接收端一定无听感影响。",
    }


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
            previous_packet = packet_by_seq[prev]
            next_packet = packet_by_seq[current]
            events.append(RtpEvent(
                type="BURST_LOSS" if gap > 1 else "PACKET_LOSS",
                start_time=next_packet.timestamp,
                severity="HIGH" if gap >= high_loss_threshold else "MEDIUM",
                details={
                    "previous_sequence_ext": prev,
                    "next_sequence_ext": current,
                    "lost_packets": gap,
                    "missing_sequence_ext_start": prev + 1,
                    "missing_sequence_ext_end": current - 1,
                    "previous_frame_number": previous_packet.frame_number,
                    "next_frame_number": next_packet.frame_number,
                    "previous_timestamp": previous_packet.timestamp,
                    "next_timestamp": next_packet.timestamp,
                    "frame_evidence_note": "丢失的 RTP 包本身没有可引用 Frame；previous/next Frame 是丢包边界证据。",
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
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(ordered) - 1)
    frac = pos - lo
    return ordered[lo] * (1 - frac) + ordered[hi] * frac


def _stream_id(key: tuple) -> str:
    src_ip, src_port, dst_ip, dst_port, ssrc = key
    return f"{src_ip}:{src_port}>{dst_ip}:{dst_port}/ssrc={ssrc}"
