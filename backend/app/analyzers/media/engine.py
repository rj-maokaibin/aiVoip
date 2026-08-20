from __future__ import annotations

import json
from pathlib import Path

from app.analyzers.candidate_gate import CandidateDecision, build_diagnostic_candidates, candidate_summary
from .engine_core import MediaIntelligenceEngine as _CoreMediaIntelligenceEngine


class MediaIntelligenceEngine(_CoreMediaIntelligenceEngine):
    """Media engine with V1 diagnostic-candidate context gates.

    Raw detector outputs stay in the core result for audit/replay. The wrapper
    adds RTP activity evidence and applies deterministic Negative Controls before
    CLICK_POP / UNEXPECTED_SILENCE may enter promoted cross-layer anomalies.
    """

    analyzer_version = "0.5.0"

    def analyze_pcap(self, path: str | Path, output_dir: str | Path) -> dict:
        result = super().analyze_pcap(path, output_dir)
        result["rtp_activity_profiles"] = self._load_rtp_activity_profiles(result)
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
    def _load_rtp_activity_profiles(result: dict) -> list[dict]:
        """Bind deterministic waveform RMS bins to RTP stream/time provenance.

        The core analyzer already creates WAVEFORM_JSON artifacts. Reading those
        just-created files avoids re-decoding RTP while giving the silence gate
        positive evidence that the counterpart RTP actually carried energy in
        the candidate window.
        """
        tracks = {x.get("stream_id"): x for x in result.get("rtp_audio_tracks", []) or []}
        out = []
        for artifact in result.get("artifacts", []) or []:
            if artifact.get("type") != "WAVEFORM_JSON":
                continue
            meta = artifact.get("metadata") or {}
            stream_id = meta.get("stream_id")
            track = tracks.get(stream_id)
            local_path = artifact.get("local_path")
            if not stream_id or not track or not local_path:
                continue
            try:
                waveform = json.loads(Path(local_path).read_text(encoding="utf-8"))
            except Exception:
                continue
            out.append({
                "stream_id": stream_id,
                "start_time": track.get("start_time"),
                "end_time": track.get("end_time"),
                "codec": track.get("codec"),
                "waveform": waveform,
                "source_artifact": artifact.get("filename"),
            })
        return out

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
