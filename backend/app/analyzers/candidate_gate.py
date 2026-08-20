from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any

from app.analyzers.profile import get_default_analyzer_profile


class CandidateDecision(StrEnum):
    PENDING = "PENDING"
    ACCEPT = "ACCEPT"
    SUPPRESS = "SUPPRESS"
    INCONCLUSIVE = "INCONCLUSIVE"
    MERGED = "MERGED"


DTMF_TRANSIENT_OVERLAP = "DTMF_TRANSIENT_OVERLAP"
NO_DTMF_OVERLAP = "NO_DTMF_OVERLAP"
ACTIVE_MEDIA_SCOPED = "ACTIVE_MEDIA_SCOPED"
RTP_MAPPING_MISSING = "RTP_MAPPING_MISSING"
RTP_MAPPING_LOW_CONFIDENCE = "RTP_MAPPING_LOW_CONFIDENCE"
COUNTERPART_RTP_SILENCE = "COUNTERPART_RTP_SILENCE"
COUNTERPART_RTP_NOT_SILENT = "COUNTERPART_RTP_NOT_SILENT"
COUNTERPART_RTP_TRACK_MISSING = "COUNTERPART_RTP_TRACK_MISSING"


AUDIO_CANDIDATE_TYPES = {"CLICK_POP", "UNEXPECTED_SILENCE"}


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _candidate_id(event: dict) -> str:
    material = {
        "type": event.get("type"),
        "time": event.get("time"),
        "scope": event.get("scope") or {},
        "details": {
            k: (event.get("details") or {}).get(k)
            for k in ("time_seconds", "start_seconds", "end_seconds", "duration_ms", "jump", "confidence")
        },
    }
    return "cand-" + hashlib.sha256(_canonical(material).encode("utf-8")).hexdigest()[:24]


def _source_audio_events(media: dict | None) -> list[tuple[int, dict]]:
    media = media or {}
    preferred = media.get("active_media_audio_events") or []
    if preferred:
        rows = list(enumerate(preferred))
    else:
        rows = [
            (index, event)
            for index, event in enumerate(media.get("cross_layer_events", []) or [])
            if str(event.get("type") or "") in AUDIO_CANDIDATE_TYPES
        ]
    seen: set[str] = set()
    out: list[tuple[int, dict]] = []
    for index, event in rows:
        key = _canonical({"type": event.get("type"), "time": event.get("time"), "scope": event.get("scope")})
        if key in seen:
            continue
        seen.add(key)
        out.append((index, event))
    return out


def _pcm_session_lookup(pcm: dict | None) -> dict[tuple[str, int], dict]:
    out: dict[tuple[str, int], dict] = {}
    for stream in (pcm or {}).get("streams", []) or []:
        tap = str((stream.get("tap") or {}).get("name") or "")
        for session in stream.get("sessions", []) or []:
            try:
                idx = int(session.get("session_index"))
            except (TypeError, ValueError):
                continue
            out[(tap, idx)] = session
    return out


def _dtmf_intervals(session: dict | None, *, guard_seconds: float) -> list[dict]:
    if not session or session.get("start_time") is None:
        return []
    base = float(session["start_time"])
    out = []
    for event in session.get("dtmf_events", []) or []:
        try:
            start = base + float(event.get("start_seconds") or 0.0) - guard_seconds
            end = base + float(event.get("end_seconds") if event.get("end_seconds") is not None else event.get("start_seconds") or 0.0) + guard_seconds
        except (TypeError, ValueError):
            continue
        out.append({"start": start, "end": end, "digit": event.get("digit"), "confidence": event.get("confidence")})
    return out


def _event_window(event: dict) -> tuple[float | None, float | None]:
    try:
        start = float(event.get("time"))
    except (TypeError, ValueError):
        return None, None
    details = event.get("details") or {}
    if event.get("type") == "UNEXPECTED_SILENCE":
        try:
            duration = float(details.get("duration_ms") or 0.0) / 1000.0
        except (TypeError, ValueError):
            duration = 0.0
        return start, start + max(0.0, duration)
    return start, start


def _overlaps(a0: float, a1: float, b0: float, b1: float) -> bool:
    return max(a0, b0) <= min(a1, b1)


def _best_rtp_mapping(media: dict | None, scope: dict) -> dict | None:
    tap = scope.get("pcm_tap")
    session_index = scope.get("pcm_session_index")
    candidates = []
    for row in (media or {}).get("correlations", []) or []:
        details = row.get("details") or {}
        if details.get("pcm_tap") != tap or details.get("pcm_session_index") != session_index:
            continue
        corr = details.get("correlation") or {}
        try:
            score = float(corr.get("absolute_correlation") or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        candidates.append((score, details))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    score, details = candidates[0]
    return {"score": score, "details": details}


def _rtp_track(media: dict | None, stream_id: str | None) -> dict | None:
    if not stream_id:
        return None
    return next((x for x in (media or {}).get("rtp_audio_tracks", []) or [] if x.get("stream_id") == stream_id), None)


def _rtp_silence_overlap(track: dict, start: float, end: float) -> dict | None:
    if track.get("start_time") is None:
        return None
    base = float(track["start_time"])
    for event in track.get("silence_events", []) or []:
        try:
            a = base + float(event.get("start_seconds") or 0.0)
            b = base + float(event.get("end_seconds") if event.get("end_seconds") is not None else event.get("start_seconds") or 0.0)
        except (TypeError, ValueError):
            continue
        if _overlaps(start, end, a, b):
            return {"start": a, "end": b, "duration_ms": event.get("duration_ms")}
    return None


def _base_candidate(event: dict, *, source_index: int) -> dict:
    start, end = _event_window(event)
    return {
        "candidate_id": _candidate_id(event),
        "type": str(event.get("type") or "AUDIO_CANDIDATE"),
        "decision": CandidateDecision.PENDING.value,
        "reason_codes": [ACTIVE_MEDIA_SCOPED],
        "severity": event.get("severity") or "MEDIUM",
        "evidence_level": event.get("evidence_level") or "L3",
        "time_range": {"start": start, "end": end, "representative": start},
        "scope": dict(event.get("scope") or {}),
        "metrics": dict(event.get("details") or {}),
        "context": {},
        "source_event_ref": {"source": "media.active_media_audio_events", "index": source_index},
    }


def _gate_click(candidate: dict, pcm: dict | None) -> dict:
    cfg = get_default_analyzer_profile().section("click_pop")
    guard = float(cfg.get("guard_ms") or 25.0) / 1000.0
    scope = candidate.get("scope") or {}
    key = (str(scope.get("pcm_tap") or ""), int(scope.get("pcm_session_index") or 0))
    session = _pcm_session_lookup(pcm).get(key)
    when = (candidate.get("time_range") or {}).get("representative")
    intervals = _dtmf_intervals(session, guard_seconds=guard)
    overlap = None
    if when is not None:
        overlap = next((x for x in intervals if x["start"] <= float(when) <= x["end"]), None)
    candidate["context"].update({"dtmf_guard_ms": round(guard * 1000.0, 3), "dtmf_event_count": len(intervals)})
    if overlap:
        candidate["decision"] = CandidateDecision.SUPPRESS.value
        candidate["reason_codes"].append(DTMF_TRANSIENT_OVERLAP)
        candidate["context"]["overlapping_dtmf"] = overlap
        candidate["metrics"]["candidate_decision"] = candidate["decision"]
        candidate["metrics"]["candidate_reason_codes"] = list(candidate["reason_codes"])
        return candidate
    candidate["decision"] = CandidateDecision.ACCEPT.value
    candidate["reason_codes"].append(NO_DTMF_OVERLAP)
    candidate["metrics"]["candidate_decision"] = candidate["decision"]
    candidate["metrics"]["candidate_reason_codes"] = list(candidate["reason_codes"])
    return candidate


def _gate_silence(candidate: dict, media: dict | None) -> dict:
    scope = candidate.get("scope") or {}
    mapping = _best_rtp_mapping(media, scope)
    min_corr = float(get_default_analyzer_profile().section("correlation")["medium_quality"])
    if mapping is None:
        candidate["decision"] = CandidateDecision.INCONCLUSIVE.value
        candidate["reason_codes"].append(RTP_MAPPING_MISSING)
        candidate["metrics"]["candidate_decision"] = candidate["decision"]
        candidate["metrics"]["candidate_reason_codes"] = list(candidate["reason_codes"])
        return candidate
    details = mapping["details"]
    stream_id = details.get("rtp_stream_id")
    candidate["context"]["counterpart_rtp_stream_id"] = stream_id
    candidate["context"]["pcm_rtp_absolute_correlation"] = round(float(mapping["score"]), 6)
    candidate["context"]["required_mapping_correlation"] = min_corr
    if float(mapping["score"]) < min_corr:
        candidate["decision"] = CandidateDecision.INCONCLUSIVE.value
        candidate["reason_codes"].append(RTP_MAPPING_LOW_CONFIDENCE)
        candidate["metrics"]["candidate_decision"] = candidate["decision"]
        candidate["metrics"]["candidate_reason_codes"] = list(candidate["reason_codes"])
        return candidate
    track = _rtp_track(media, stream_id)
    if track is None:
        candidate["decision"] = CandidateDecision.INCONCLUSIVE.value
        candidate["reason_codes"].append(COUNTERPART_RTP_TRACK_MISSING)
        candidate["metrics"]["candidate_decision"] = candidate["decision"]
        candidate["metrics"]["candidate_reason_codes"] = list(candidate["reason_codes"])
        return candidate
    window = candidate.get("time_range") or {}
    start = window.get("start")
    end = window.get("end")
    if start is None or end is None:
        candidate["decision"] = CandidateDecision.INCONCLUSIVE.value
        candidate["reason_codes"].append(COUNTERPART_RTP_TRACK_MISSING)
        candidate["metrics"]["candidate_decision"] = candidate["decision"]
        candidate["metrics"]["candidate_reason_codes"] = list(candidate["reason_codes"])
        return candidate
    overlap = _rtp_silence_overlap(track, float(start), float(end))
    if overlap:
        candidate["decision"] = CandidateDecision.SUPPRESS.value
        candidate["reason_codes"].append(COUNTERPART_RTP_SILENCE)
        candidate["context"]["counterpart_silence_window"] = overlap
    else:
        candidate["decision"] = CandidateDecision.ACCEPT.value
        candidate["reason_codes"].append(COUNTERPART_RTP_NOT_SILENT)
    candidate["metrics"]["candidate_decision"] = candidate["decision"]
    candidate["metrics"]["candidate_reason_codes"] = list(candidate["reason_codes"])
    candidate["metrics"]["counterpart_rtp_stream_id"] = stream_id
    candidate["metrics"]["pcm_rtp_absolute_correlation"] = round(float(mapping["score"]), 6)
    return candidate


def build_diagnostic_candidates(*, pcm: dict | None, media: dict | None) -> list[dict]:
    """Turn call-scoped audio detector events into auditable diagnostic candidates.

    Raw PCM detector output remains untouched. Only Media Analyzer events already
    scoped to an active SIP media window enter this gate. A candidate must reach
    ACCEPT before the Finding layer may expose it as a user-visible anomaly.
    """
    effective_pcm = pcm or (media or {}).get("pcm") or {}
    out: list[dict] = []
    for index, event in _source_audio_events(media):
        candidate = _base_candidate(event, source_index=index)
        if candidate["type"] == "CLICK_POP":
            candidate = _gate_click(candidate, effective_pcm)
        elif candidate["type"] == "UNEXPECTED_SILENCE":
            candidate = _gate_silence(candidate, media)
        out.append(candidate)
    out.sort(key=lambda x: ((x.get("time_range") or {}).get("representative") is None, (x.get("time_range") or {}).get("representative") or 0.0, x["candidate_id"]))
    return out


def candidate_summary(candidates: list[dict]) -> dict:
    decisions = {x.value: 0 for x in CandidateDecision}
    by_type: dict[str, dict[str, int]] = {}
    for candidate in candidates:
        decision = str(candidate.get("decision") or CandidateDecision.PENDING.value)
        decisions[decision] = decisions.get(decision, 0) + 1
        ftype = str(candidate.get("type") or "UNKNOWN")
        bucket = by_type.setdefault(ftype, {})
        bucket[decision] = bucket.get(decision, 0) + 1
    return {"total": len(candidates), "decisions": decisions, "by_type": by_type}
