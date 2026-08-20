from __future__ import annotations

import json
from pathlib import Path

from app.analyzers.candidate_gate import CandidateDecision, build_diagnostic_candidates, candidate_summary
from app.analyzers.pcm.wav import write_wav
from .engine_core import MediaIntelligenceEngine as _CoreMediaIntelligenceEngine, _artifact


class MediaIntelligenceEngine(_CoreMediaIntelligenceEngine):
    """Media engine with candidate gates and candidate-bound evidence clips."""

    analyzer_version = "0.6.0"

    def analyze_pcap(self, path: str | Path, output_dir: str | Path) -> dict:
        output_dir = Path(output_dir)
        result = super().analyze_pcap(path, output_dir)
        self._annotate_source_artifact_time_ranges(result)
        result["rtp_activity_profiles"] = self._load_rtp_activity_profiles(result)
        candidates = build_diagnostic_candidates(pcm=result.get("pcm"), media=result)
        accepted = [x for x in candidates if x.get("decision") == CandidateDecision.ACCEPT.value]

        candidate_artifacts = self._write_candidate_pcm_clips(path, output_dir, accepted)
        if candidate_artifacts:
            result.setdefault("artifacts", []).extend(candidate_artifacts)

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
        summary["candidate_audio_clip_count"] = len(candidate_artifacts)
        summary["artifact_count"] = len(result.get("artifacts") or [])
        return result

    @staticmethod
    def _annotate_source_artifact_time_ranges(result: dict) -> None:
        rtp = {x.get("stream_id"): x for x in result.get("rtp_audio_tracks", []) or []}
        pcm = {(x.get("pcm_tap"), x.get("session_index")): x for x in result.get("pcm_audio_tracks", []) or []}
        for artifact in result.get("artifacts", []) or []:
            meta = artifact.setdefault("metadata", {})
            stream_id = meta.get("stream_id")
            if stream_id in rtp:
                track = rtp[stream_id]
                meta.setdefault("start_time", track.get("start_time"))
                meta.setdefault("end_time", track.get("end_time"))
                meta.setdefault("codec", track.get("codec"))
            key = (meta.get("pcm_tap"), meta.get("session_index"))
            if key in pcm:
                track = pcm[key]
                meta.setdefault("start_time", track.get("start_time"))
                meta.setdefault("end_time", track.get("end_time"))
                meta.setdefault("direction", track.get("direction"))

    @staticmethod
    def _load_rtp_activity_profiles(result: dict) -> list[dict]:
        """Bind deterministic waveform RMS bins to RTP stream/time provenance."""
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

    def _write_candidate_pcm_clips(self, path: str | Path, output_dir: Path, candidates: list[dict]) -> list[dict]:
        """Generate exactly scoped PCM clips for ACCEPT audio candidates.

        Core raw clips remain useful diagnostic artifacts, but this method gives
        FindingEvidencePackage a stable clip carrying candidate_id and the exact
        accepted time window. Click uses ±0.5s context; Silence uses ±1s around
        the full event window, matching the V1 evidence-report clip contract.
        """
        wanted = [x for x in candidates if x.get("type") in {"CLICK_POP", "UNEXPECTED_SILENCE"}]
        if not wanted:
            return []
        signals = self._extract_pcm_signals(path)
        lookup = {(x["tap"]["name"], int(x["session_index"])): x for x in signals}
        artifacts = []
        for candidate in wanted:
            scope = candidate.get("scope") or {}
            try:
                key = (str(scope.get("pcm_tap") or ""), int(scope.get("pcm_session_index")))
            except (TypeError, ValueError):
                continue
            signal = lookup.get(key)
            window = candidate.get("time_range") or {}
            if not signal or window.get("start") is None:
                continue
            try:
                event_start = float(window["start"])
                event_end = float(window.get("end") if window.get("end") is not None else event_start)
            except (TypeError, ValueError):
                continue
            pre = 0.5 if candidate.get("type") == "CLICK_POP" else 1.0
            post = 0.5 if candidate.get("type") == "CLICK_POP" else 1.0
            clip_start = max(float(signal["start_time"]), event_start - pre)
            clip_end = min(float(signal["end_time"]), event_end + post)
            sr = int(signal["sample_rate"])
            a = max(0, int(round((clip_start - float(signal["start_time"])) * sr)))
            b = min(signal["samples"].size, int(round((clip_end - float(signal["start_time"])) * sr)))
            if b <= a:
                continue
            cid = str(candidate.get("candidate_id") or "candidate").replace("/", "_")
            ftype = str(candidate.get("type") or "AUDIO").upper()
            wav = output_dir / f"candidate_{cid}_{ftype}.wav"
            write_wav(wav, signal["samples"][a:b], sr, 1)
            artifacts.append(_artifact(wav, "AUDIO_CLIP", "audio/wav", {
                "event_type": ftype,
                "event_time": event_start,
                "candidate_id": candidate.get("candidate_id"),
                "candidate_decision": candidate.get("decision"),
                "clip_role": "CANDIDATE_PRIMARY",
                "pcm_tap": key[0],
                "session_index": key[1],
                "start_time": clip_start,
                "end_time": clip_end,
                "event_start_time": event_start,
                "event_end_time": event_end,
                "pre_context_seconds": pre,
                "post_context_seconds": post,
            }))
        return artifacts

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
