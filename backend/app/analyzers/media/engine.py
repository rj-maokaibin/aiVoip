from __future__ import annotations

from pathlib import Path

from app.analyzers.candidate_gate import CandidateDecision, build_diagnostic_candidates, candidate_summary
from .engine_core import MediaIntelligenceEngine as _CoreMediaIntelligenceEngine


class MediaIntelligenceEngine(_CoreMediaIntelligenceEngine):
    """Media engine with V1 diagnostic-candidate context gates.

    The core engine keeps raw detector output unchanged for audit/replay. This
    wrapper applies deterministic negative controls after Call-scoped media
    events have been built, then exposes only ACCEPT candidates as promoted
    CLICK_POP / UNEXPECTED_SILENCE cross-layer anomalies.
    """

    analyzer_version = "0.5.0"

    def analyze_pcap(self, path: str | Path, output_dir: str | Path) -> dict:
        result = super().analyze_pcap(path, output_dir)
        candidates = build_diagnostic_candidates(pcm=result.get("pcm"), media=result)
        accepted = [x for x in candidates if x.get("decision") == CandidateDecision.ACCEPT.value]

        non_audio = [
            event for event in result.get("cross_layer_events", []) or []
            if str(event.get("type") or "") not in {"CLICK_POP", "UNEXPECTED_SILENCE"}
        ]
        promoted = [self._candidate_event(x) for x in accepted]
        result["cross_layer_events"] = non_audio + promoted
        result["active_media_audio_events"] = promoted
        result["diagnostic_candidates"] = candidates
        result["diagnostic_candidate_summary"] = candidate_summary(candidates)
        result["version"] = self.analyzer_version

        summary = result.setdefault("summary", {})
        summary["unexpected_silence_count"] = sum(1 for x in accepted if x.get("type") == "UNEXPECTED_SILENCE")
        summary["click_pop_count"] = sum(1 for x in accepted if x.get("type") == "CLICK_POP")
        summary["diagnostic_candidate_count"] = len(candidates)
        summary["diagnostic_candidate_accepted_count"] = len(accepted)
        summary["diagnostic_candidate_suppressed_count"] = sum(
            1 for x in candidates if x.get("decision") == CandidateDecision.SUPPRESS.value
        )
        summary["diagnostic_candidate_inconclusive_count"] = sum(
            1 for x in candidates if x.get("decision") == CandidateDecision.INCONCLUSIVE.value
        )
        return result

    @staticmethod
    def _candidate_event(candidate: dict) -> dict:
        window = candidate.get("time_range") or {}
        details = dict(candidate.get("metrics") or {})
        details.update({
            "candidate_id": candidate.get("candidate_id"),
            "candidate_decision": candidate.get("decision"),
            "candidate_reason_codes": list(candidate.get("reason_codes") or []),
            "candidate_context": dict(candidate.get("context") or {}),
            "interpretation": (
                "Call 级上下文与跨层 Negative Control 已通过；该事件可作为初步证据 Finding，仍不等于最终根因。"
            ),
        })
        return {
            "type": candidate.get("type"),
            "time": window.get("representative"),
            "start_time": window.get("start"),
            "end_time": window.get("end"),
            "severity": candidate.get("severity") or "MEDIUM",
            "evidence_level": candidate.get("evidence_level") or "L3",
            "scope": dict(candidate.get("scope") or {}),
            "details": details,
        }
