from __future__ import annotations

from .engine_core import PacketIntelligenceEngine as _CorePacketIntelligenceEngine
from .incidents import build_rtp_incidents, enrich_packet_anomalies, incident_summary


class PacketIntelligenceEngine(_CorePacketIntelligenceEngine):
    """Packet Analyzer with stable RTP Incident semantics layered over raw events."""

    analyzer_version = "0.5.0"

    def analyze_packets(self, packets):
        result = super().analyze_packets(packets)
        incidents = build_rtp_incidents(result.get("calls"), result.get("rtp_streams"))
        result["rtp_incidents"] = incidents
        result["rtp_incident_summary"] = incident_summary(incidents)
        result["anomalies"] = enrich_packet_anomalies(result.get("anomalies"), incidents)
        result["version"] = self.analyzer_version
        summary = result.setdefault("summary", {})
        summary["rtp_incident_count"] = len(incidents)
        summary["high_delta_incident_count"] = int(result["rtp_incident_summary"]["by_type"].get("HIGH_DELTA", 0))
        summary["cadence_stall_without_sequence_gap_count"] = int(
            result["rtp_incident_summary"].get("cadence_stall_without_sequence_gap_count", 0)
        )
        return result
