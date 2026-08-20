from __future__ import annotations

import hashlib
import json
import math
from enum import StrEnum
from typing import Any

from app.analyzers.profile import AnalyzerProfileError, get_default_analyzer_profile


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
COUNTERPART_RTP_ACTIVE = "COUNTERPART_RTP_ACTIVE"
COUNTERPART_RTP_LOW_ENERGY = "COUNTERPART_RTP_LOW_ENERGY"
COUNTERPART_RTP_ACTIVITY_AMBIGUOUS = "COUNTERPART_RTP_ACTIVITY_AMBIGUOUS"
COUNTERPART_RTP_ACTIVITY_UNAVAILABLE = "COUNTERPART_RTP_ACTIVITY_UNAVAILABLE"
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


def _gate_config() -> dict:
    profile = get_default_analyzer_profile()
    try:
        return dict(profile.section("candidate_gate"))
    except AnalyzerProfileError:
        return {
            "click_dtmf_guard_ms": float(profile.section("click_pop").get("guard_ms") or 25.0),
            "silence_mapping_min_correlation": float(profile.section("correlation")["medium_quality"]),
            "silence_counterpart_active_margin_db": 6.0,
            "silence_counterpart_active_fraction_min": 0.20,
            "silence_counterpart_quiet_fraction_max": 0.05,
            "silence_counterpart_min_bins": 2,
        }


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


def _rtp_activity_profile(media: dict | None, stream_id: str | None) -> dict | None:
    if not stream_id:
        return None
    return next((x for x in (media or {}).get("rtp_activity_profiles", []) or [] if x.get("stream_id") == stream_id), None)


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


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return -120.0
    vals = sorted(values)
    if len(vals) == 1:
        return vals[0]
    pos = max(0.0, min(100.0, pct)) / 100.0 * (len(vals) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return vals[lo]
    frac = pos - lo
    return vals[lo] * (1.0 - frac) + vals[hi] * frac


def _activity_threshold_dbfs(levels: list[float]) -> float:
    cfg = get_default_analyzer_profile().section("silence")
    floor = _percentile(levels, float(cfg["noise_floor_percentile"]))
    speech = _percentile(levels, float(cfg["speech_percentile"]))
    return min(
        float(cfg["threshold_max_dbfs"]),
        max(
            float(cfg["threshold_min_dbfs"]),
            min(floor + float(cfg["noise_floor_margin_db"]), speech - float(cfg["speech_margin_db"])),
        ),
    )


def _classify_rtp_activity(profile: dict, start: float, end: float) -> dict:
    waveform = profile.get("waveform") or {}
    bins = waveform.get("bins") or []
    if profile.get("start_time") is None or not bins:
        return {"status": "UNAVAILABLE"}
    base = float(profile["start_time"])
    sample_rate = float(waveform.get("sample_rate") or 0.0)
    bin_size = float(waveform.get("bin_size_samples") or 0.0)
    width = bin_size / sample_rate if sample_rate > 0 and bin_size > 0 else 0.0
    all_levels = [float(x.get("rms_dbfs")) for x in bins if isinstance(x.get("rms_dbfs"), (int, float))]
    selected = []
    for item in bins:
        level = item.get("rms_dbfs")
        if not isinstance(level, (int, float)):
            continue
        rel = float(item.get("t") or 0.0)
        a = base + rel
        b = a + max(width, 1e-6)
        if _overlaps(start, end, a, b):
            selected.append(float(level))
    gate = _gate_config()
    min_bins = int(gate.get("silence_counterpart_min_bins") or 2)
    if len(selected) < min_bins or not all_levels:
        return {"status": "UNAVAILABLE", "selected_bin_count": len(selected), "required_bin_count": min_bins}
    threshold = _activity_threshold_dbfs(all_levels)
    active_level = threshold + float(gate.get("silence_counterpart_active_margin_db") or 6.0)
    active_count = sum(1 for x in selected if x >= active_level)
    active_fraction = active_count / len(selected)
    median = _percentile(selected, 50.0)
    peak = max(selected)
    active_fraction_min = float(gate.get("silence_counterpart_active_fraction_min") or 0.20)
    quiet_fraction_max = float(gate.get("silence_counterpart_quiet_fraction_max") or 0.05)
    if active_fraction >= active_fraction_min:
        status = "ACTIVE"
    elif active_fraction <= quiet_fraction_max and median <= threshold:
        status = "LOW_ENERGY"
    else:
        status = "AMBIGUOUS"
    return {
        "status": status,
        "threshold_dbfs": round(threshold, 3),
        "active_level_dbfs": round(active_level, 3),
        "median_dbfs": round(median, 3),
        "peak_dbfs": round(peak, 3),
        "active_fraction": round(active_fraction, 6),
        "selected_bin_count": len(selected),
        "source_artifact": profile.get("source_artifact"),
    }


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


def _finalize(candidate: dict) -> dict:
    candidate["metrics"]["candidate_decision"] = candidate["decision"]
    candidate["metrics"]["candidate_reason_codes"] = list(candidate["reason_codes"])
    return candidate


def _gate_click(candidate: dict, pcm: dict | None) -> dict:
    cfg = _gate_config()
    guard = float(cfg.get("click_dtmf_guard_ms") or 25.0) / 1000.0
    scope = candidate.get("scope") or {}
    try:
        session_index = int(scope.get("pcm_session_index"))
    except (TypeError, ValueError):
        session_index = 0
    key = (str(scope.get("pcm_tap") or ""), session_index)
    session = _pcm_session_lookup(pcm).get(key)
    when = (candidate.get("time_range") or {}).get("representative")
    intervals = _dtmf_intervals(session, guard_seconds=guard)
    overlap = next((x for x in intervals if when is not None and x["start"] <= float(when) <= x["end"]), None)
    candidate["context"].update({"dtmf_guard_ms": round(guard * 1000.0, 3), "dtmf_event_count": len(intervals)})
    if overlap:
        candidate["decision"] = CandidateDecision.SUPPRESS.value
        candidate["reason_codes"].append(DTMF_TRANSIENT_OVERLAP)
        candidate["context"]["overlapping_dtmf"] = overlap
    else:
        candidate["decision"] = CandidateDecision.ACCEPT.value
        candidate["reason_codes"].append(NO_DTMF_OVERLAP)
    return _finalize(candidate)


def _gate_silence(candidate: dict, media: dict | None) -> dict:
    scope = candidate.get("scope") or {}
    mapping = _best_rtp_mapping(media, scope)
    gate_cfg = _gate_config()
    min_corr = float(gate_cfg.get("silence_mapping_min_correlation") or get_default_analyzer_profile().section("correlation")["medium_quality"])
    if mapping is None:
        candidate["decision"] = CandidateDecision.INCONCLUSIVE.value
        candidate["reason_codes"].append(RTP_MAPPING_MISSING)
        return _finalize(candidate)
    details = mapping["details"]
    stream_id = details.get("rtp_stream_id")
    candidate["context"]["counterpart_rtp_stream_id"] = stream_id
    candidate["context"]["pcm_rtp_absolute_correlation"] = round(float(mapping["score"]), 6)
    candidate["context"]["required_mapping_correlation"] = min_corr
    if float(mapping["score"]) < min_corr:
        candidate["decision"] = CandidateDecision.INCONCLUSIVE.value
        candidate["reason_codes"].append(RTP_MAPPING_LOW_CONFIDENCE)
        return _finalize(candidate)
    track = _rtp_track(media, stream_id)
    if track is None:
        candidate["decision"] = CandidateDecision.INCONCLUSIVE.value
        candidate["reason_codes"].append(COUNTERPART_RTP_TRACK_MISSING)
        return _finalize(candidate)
    window = candidate.get("time_range") or {}
    start, end = window.get("start"), window.get("end")
    if start is None or end is None:
        candidate["decision"] = CandidateDecision.INCONCLUSIVE.value
        candidate["reason_codes"].append(COUNTERPART_RTP_ACTIVITY_UNAVAILABLE)
        return _finalize(candidate)

    activity_profile = _rtp_activity_profile(media, stream_id)
    if activity_profile:
        activity = _classify_rtp_activity(activity_profile, float(start), float(end))
        candidate["context"]["counterpart_rtp_activity"] = activity
        if activity.get("status") == "ACTIVE":
            candidate["decision"] = CandidateDecision.ACCEPT.value
            candidate["reason_codes"].append(COUNTERPART_RTP_ACTIVE)
        elif activity.get("status") == "LOW_ENERGY":
            candidate["decision"] = CandidateDecision.SUPPRESS.value
            candidate["reason_codes"].append(COUNTERPART_RTP_LOW_ENERGY)
        elif activity.get("status") == "AMBIGUOUS":
            candidate["decision"] = CandidateDecision.INCONCLUSIVE.value
            candidate["reason_codes"].append(COUNTERPART_RTP_ACTIVITY_AMBIGUOUS)
        else:
            candidate["decision"] = CandidateDecision.INCONCLUSIVE.value
            candidate["reason_codes"].append(COUNTERPART_RTP_ACTIVITY_UNAVAILABLE)
    else:
        # Legacy AnalyzerRun fallback: an explicit aligned RTP silence is enough
        # to suppress, but absence of such an event is not positive proof that
        # RTP carried speech. Keep it INCONCLUSIVE instead of creating a false Finding.
        overlap = _rtp_silence_overlap(track, float(start), float(end))
        if overlap:
            candidate["decision"] = CandidateDecision.SUPPRESS.value
            candidate["reason_codes"].append(COUNTERPART_RTP_SILENCE)
            candidate["context"]["counterpart_silence_window"] = overlap
        else:
            candidate["decision"] = CandidateDecision.INCONCLUSIVE.value
            candidate["reason_codes"].append(COUNTERPART_RTP_ACTIVITY_UNAVAILABLE)

    candidate["metrics"]["counterpart_rtp_stream_id"] = stream_id
    candidate["metrics"]["pcm_rtp_absolute_correlation"] = round(float(mapping["score"]), 6)
    return _finalize(candidate)


def build_diagnostic_candidates(*, pcm: dict | None, media: dict | None) -> list[dict]:
    """Turn Call-scoped detector observations into auditable candidates.

    Raw PCM detector output is never mutated. Only events already scoped to an
    active SIP media window enter this gate. A Candidate must reach ACCEPT before
    the Media/Report layers may expose it as a user-visible Finding.
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
