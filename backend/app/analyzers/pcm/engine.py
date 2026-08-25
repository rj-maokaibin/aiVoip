from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict
from pathlib import Path
import statistics

import numpy as np

from .dtmf_quality import dtmf_quality_events
from .pcap_udp import UdpDatagram, iter_udp_datagrams
from .profile import PcmProfile
from .signal import basic_stats, detect_dtmf, dtmf_sequence, hum_analysis
from app.analyzers.audio.features import spectral_tone_analysis
from app.analyzers.audio.quality import detect_unexpected_silence, detect_click_pop_robust
from app.analyzers.profile import get_default_analyzer_profile


class PcmIntelligenceEngine:
    analyzer_name = "pcm_intelligence"
    analyzer_version = "0.6.0"

    def __init__(self, profile: PcmProfile):
        self.profile = profile
        self.analyzer_profile = get_default_analyzer_profile()

    @staticmethod
    def _source_endpoints(packets: list[UdpDatagram]) -> list[dict]:
        counts: dict[tuple[str, int], int] = defaultdict(int)
        first: dict[tuple[str, int], float] = {}
        last: dict[tuple[str, int], float] = {}
        for packet in packets:
            key = (str(packet.src_ip), int(packet.src_port))
            counts[key] += 1
            first[key] = min(first.get(key, packet.timestamp), packet.timestamp)
            last[key] = max(last.get(key, packet.timestamp), packet.timestamp)
        return [
            {
                "ip": ip,
                "port": port,
                "packet_count": counts[(ip, port)],
                "first_timestamp": first[(ip, port)],
                "last_timestamp": last[(ip, port)],
            }
            for ip, port in sorted(counts, key=lambda item: (-counts[item], item[0], item[1]))
        ]

    def analyze_pcap(self, path: str | Path) -> dict:
        tap_by_port = {tap.dst_port: tap for tap in self.profile.taps}
        grouped: dict[str, list[UdpDatagram]] = defaultdict(list)
        ignored_size = 0
        for packet in iter_udp_datagrams(path):
            tap = tap_by_port.get(packet.dst_port)
            if not tap:
                continue
            if self.profile.packet_payload_bytes is not None and len(packet.payload) != self.profile.packet_payload_bytes:
                ignored_size += 1
                continue
            grouped[tap.name].append(packet)

        streams = []
        for tap in self.profile.taps:
            packets = grouped.get(tap.name, [])
            sessions = self._split_sessions(packets)
            if self.profile.can_decode:
                session_results = [self._analyze_session(i, s) for i, s in enumerate(sessions)]
            else:
                session_results = [self._raw_session(i, s) for i, s in enumerate(sessions)]
            streams.append({
                "tap": asdict(tap),
                "packet_count": len(packets),
                "source_endpoints": self._source_endpoints(packets),
                "sessions": session_results,
            })

        source_ip_counts: dict[str, int] = defaultdict(int)
        for stream in streams:
            for endpoint in stream.get("source_endpoints", []):
                source_ip_counts[str(endpoint["ip"])] += int(endpoint["packet_count"])
        source_ips = [
            {"ip": ip, "packet_count": count}
            for ip, count in sorted(source_ip_counts.items(), key=lambda item: (-item[1], item[0]))
        ]

        return {
            "analyzer": self.analyzer_name,
            "version": self.analyzer_version,
            "status": "SUCCESS" if self.profile.can_decode else "PARTIAL_SUCCESS",
            "profile_id": self.profile.id,
            "pcm_profile": self.profile.metadata(),
            "analyzer_profile": self.analyzer_profile.metadata(),
            "format_availability": "AVAILABLE" if self.profile.can_decode else "UNAVAILABLE",
            "format": {
                "sample_rate": self.profile.sample_rate,
                "bit_depth": self.profile.bit_depth,
                "signed": self.profile.signed,
                "endian": self.profile.endian,
                "channels": self.profile.channels,
                "udp_payload_bytes": self.profile.packet_payload_bytes,
                "payload_offset": self.profile.payload_offset,
                "pcm_payload_bytes": self.profile.decoded_payload_bytes,
                "expected_packet_interval_ms": self.profile.expected_packet_interval_ms,
            },
            "summary": {
                "tap_count": len([s for s in streams if s["packet_count"]]),
                "total_packets": sum(s["packet_count"] for s in streams),
                "session_count": sum(len(s["sessions"]) for s in streams),
                "ignored_size_mismatch_packets": ignored_size,
                "source_ips": source_ips,
                "source_ip_count": len(source_ips),
            },
            "streams": streams,
        }

    def _split_sessions(self, packets: list[UdpDatagram]) -> list[list[UdpDatagram]]:
        if not packets:
            return []
        packets = sorted(packets, key=lambda p: (p.timestamp, p.frame_number))
        out: list[list[UdpDatagram]] = [[packets[0]]]
        for packet in packets[1:]:
            if (packet.timestamp - out[-1][-1].timestamp) * 1000.0 > self.profile.session_gap_ms:
                out.append([])
            out[-1].append(packet)
        return out

    def _payload_bytes(self, packet: UdpDatagram) -> bytes:
        payload = packet.payload[int(self.profile.payload_offset):]
        expected = self.profile.pcm_payload_bytes
        return payload[:expected] if expected is not None else payload

    def _raw_session(self, index: int, packets: list[UdpDatagram]) -> dict:
        intervals = [(b.timestamp - a.timestamp) * 1000.0 for a, b in zip(packets, packets[1:])]
        return {
            "session_index": index,
            "start_time": packets[0].timestamp,
            "end_time": packets[-1].timestamp,
            "packet_count": len(packets),
            "source_endpoints": self._source_endpoints(packets),
            "payload_bytes": sum(len(p.payload) for p in packets),
            "analysis_availability": "UNAVAILABLE",
            "unavailable_reason": "PCM_FORMAT_NOT_VERIFIED",
            "median_packet_interval_ms": round(statistics.median(intervals), 6) if intervals else None,
            "max_packet_interval_ms": round(max(intervals), 6) if intervals else None,
        }

    def _analyze_session(self, index: int, packets: list[UdpDatagram]) -> dict:
        raw = b"".join(self._payload_bytes(p) for p in packets)
        itemsize = np.dtype(self.profile.dtype).itemsize
        if len(raw) % itemsize:
            raw = raw[: len(raw) - (len(raw) % itemsize)]
        samples = np.frombuffer(raw, dtype=self.profile.dtype).copy()
        intervals = [(b.timestamp - a.timestamp) * 1000.0 for a, b in zip(packets, packets[1:])]
        expected = self.profile.expected_packet_interval_ms
        gap_events = []
        if expected is not None:
            gap_events = [
                {"time": b.timestamp, "delta_ms": round(delta, 6), "excess_ms": round(max(0.0, delta - expected), 6)}
                for (a, b), delta in zip(zip(packets, packets[1:]), intervals)
                if delta >= max(expected * 3.0, expected + 20.0)
            ]
        assert self.profile.sample_rate is not None and self.profile.channels is not None
        dtmf_events = detect_dtmf(samples, self.profile.sample_rate)
        quality_events = dtmf_quality_events(dtmf_events)
        return {
            "session_index": index,
            "start_time": packets[0].timestamp,
            "end_time": packets[-1].timestamp,
            "packet_count": len(packets),
            "source_endpoints": self._source_endpoints(packets),
            "payload_bytes": len(raw),
            "analysis_availability": "AVAILABLE",
            "audio_duration_seconds": round(samples.size / self.profile.sample_rate / self.profile.channels, 6),
            "capture_duration_seconds": round(packets[-1].timestamp - packets[0].timestamp, 6),
            "median_packet_interval_ms": round(statistics.median(intervals), 6) if intervals else None,
            "max_packet_interval_ms": round(max(intervals), 6) if intervals else None,
            "gap_event_count": len(gap_events),
            "gap_events": gap_events,
            "signal": basic_stats(samples),
            "hum": hum_analysis(samples, self.profile.sample_rate),
            "spectral": spectral_tone_analysis(samples, self.profile.sample_rate),
            "silence_events": detect_unexpected_silence(samples, self.profile.sample_rate),
            "click_pop_events": detect_click_pop_robust(samples, self.profile.sample_rate),
            "dtmf_events": dtmf_events,
            "dtmf_sequences": dtmf_sequence(dtmf_events),
            "dtmf_quality_events": quality_events,
            "dtmf_quality_event_count": len(quality_events),
        }
