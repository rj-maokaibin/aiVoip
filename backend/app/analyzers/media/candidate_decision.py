from __future__ import annotations

import hashlib
import json
from typing import Any

from app.analyzers.profile import get_default_analyzer_profile


CANDIDATE_DECISION_VERSION = "candidate-decision-v1"
PROMOTED = "PROMOTED"
REJECTED_NEGATIVE_CONTROL = "REJECTED_NEGATIVE_CONTROL"
INCONCLUSIVE = "INCONCLUSIVE"


_DEFAULTS = {
    "dtmf_guard_ms": 80.0,
    "media_boundary_guard_ms": 120.0,
    "silence_counterpart_overlap_ratio": 0.50,
    "silence_min_correlation": 0.55,
}


def _cfg() -> dict[str, float]:
    profile = get_default_analyzer_profile()
    raw = profile.config.get("candidate_decision") or {}
    return {key: float(raw.get(key, value)) for key, value in _DEFAULTS.items()}


def _stable_id(payload: dict[str, Any]) -> str:
    material = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return "candidate-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:20]


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def _event_window(event: dict) -> tuple[float, float]:
    start = float(event.get("time") or 0.0)
    details = event.get("details") or {}
    if event.get("type") == "UNEXPECTED_SILENCE":
        duration = float(details.get("duration_ms") or 0.0) / 1000.0
        end = float(details.get("absolute_end_time") or (start + duration))
        return start, max(start, end)
    return start, start


def _pcm_session(media: dict, tap: str | None, session_index: Any) -> dict | None:
    pcm = media.get("pcm") or {}
    for stream in pcm.get("streams", []) or []:
        if str((stream.get("tap") or {}).get("name")) != str(tap):
            continue
        for session in stream.get("sessions", []) or []:
            if session.get("session_index") == session_index:
                return session
    return None


def _dtmf_negative_control(media: dict, event: dict, cfg: dict[str, float]) -> dict | None:
    scope = event.get("scope") or {}
    session = _pcm_session(media, scope.get("pcm_tap"), scope.get("pcm_session_index"))
    if not session:
        return None
    session_start = float(session.get("start_time") or 0.0)
    event_time = float(event.get("time") or 0.0)
    guard = cfg["dtmf_guard_ms"] / 1000.0
    for dtmf in session.get("dtmf_events", []) or []:
        start = session_start + float(dtmf.get("start_seconds") or 0.0) - guard
        end = session_start + float(dtmf.get("end_seconds") or dtmf.get("start_seconds") or 0.0) + guard
        if start <= event_time <= end:
            return {
                "type": "DTMF_OVERLAP",
                "status": "MATCHED",
                "digit": dtmf.get("digit"),
                "dtmf_start_time": round(start + guard, 6),
                "dtmf_end_time": round(end - guard, 6),
                "guard_ms": cfg["dtmf_guard_ms"],
            }
    return None


def _media_boundary_negative_control(event: dict, cfg: dict[str, float]) -> dict | None:
    window = (event.get("scope") or {}).get("active_media_window") or {}
    if window.get("start_time") is None or window.get("end_time") is None:
        return None
    when = float(event.get("time") or 0.0)
    guard = cfg["media_boundary_guard_ms"] / 1000.0
    start = float(window["start_time"])
    end = float(window["end_time"])
    if abs(when - start) <= guard or abs(end - when) <= guard:
        return {
            "type": "MEDIA_BOUNDARY_TRANSIENT",
            "status": "MATCHED",
            "media_start_time": start,
            "media_end_time": end,
            "guard_ms": cfg["media_boundary_guard_ms"],
        }
    return None


def _best_rtp_correlation(media: dict, event: dict) -> dict | None:
    scope = event.get("scope") or {}
    tap = scope.get("pcm_tap")
    session_index = scope.get("pcm_session_index")
    matches = []
    for corr_event in media.get("correlations", []) or []:
        details = corr_event.get("details") or {}
        if details.get("pcm_tap") != tap or details.get("pcm_session_index") != session_index:
            continue
        corr = details.get("correlation") or {}
        matches.append((float(corr.get("absolute_correlation") or 0.0), details))
    return max(matches, key=lambda x: x[0])[1] if matches else None


def _track(media: dict, stream_id: str | None) -> dict | None:
    for item in media.get("rtp_audio_tracks", []) or []:
        if item.get("stream_id") == stream_id:
            return item
    return None


def _rtp_silence_negative_control(media: dict, event: dict, cfg: dict[str, float]) -> tuple[dict | None, dict | None]:
    corr = _best_rtp_correlation(media, event)
    if not corr:
        return None, {"reason_code": "NO_DIRECTIONAL_RTP_REFERENCE"}
    correlation = corr.get("correlation") or {}
    score = float(correlation.get("absolute_correlation") or 0.0)
    if score < cfg["silence_min_correlation"]:
        return None, {
            "reason_code": "RTP_REFERENCE_CORRELATION_TOO_LOW",
            "rtp_stream_id": corr.get("rtp_stream_id"),
            "absolute_correlation": score,
        }
    track = _track(media, corr.get("rtp_stream_id"))
    if not track:
        return None, {"reason_code": "RTP_AUDIO_TRACK_UNAVAILABLE", "rtp_stream_id": corr.get("rtp_stream_id")}

    start, end = _event_window(event)
    duration = max(1e-9, end - start)
    best_ratio = 0.0
    best = None
    track_start = float(track.get("start_time") or 0.0)
    for silence in track.get("silence_events", []) or []:
        r0 = track_start + float(silence.get("start_seconds") or 0.0)
        r1 = track_start + float(silence.get("end_seconds") or silence.get("start_seconds") or 0.0)
        ratio = _overlap(start, end, r0, r1) / duration
        if ratio > best_ratio:
            best_ratio = ratio
            best = (r0, r1, silence)
    if best and best_ratio >= cfg["silence_counterpart_overlap_ratio"]:
        return {
            "type": "RTP_COUNTERPART_SILENCE",
            "status": "MATCHED",
            "rtp_stream_id": corr.get("rtp_stream_id"),
            "absolute_correlation": score,
            "overlap_ratio": round(best_ratio, 6),
            "rtp_silence_start_time": round(best[0], 6),
            "rtp_silence_end_time": round(best[1], 6),
        }, None

    # Absence of an RTP silence candidate is not proof of positive media energy in
    # exactly the same window. Fail closed until a window-energy timeline is present.
    return None, {
        "reason_code": "COUNTERPART_ACTIVITY_NOT_PROVEN",
        "rtp_stream_id": corr.get("rtp_stream_id"),
        "absolute_correlation": score,
    }


def decide_event(media: dict, event: dict) -> dict:
    cfg = _cfg()
    ftype = str(event.get("type") or "")
    scope = event.get("scope") or {}
    candidate_id = _stable_id({
        "type": ftype,
        "time": event.get("time"),
        "call_id": scope.get("call_id"),
        "pcm_tap": scope.get("pcm_tap"),
        "pcm_session_index": scope.get("pcm_session_index"),
    })
    base = {
        "schema_version": CANDIDATE_DECISION_VERSION,
        "candidate_id": candidate_id,
        "candidate_type": ftype,
        "candidate_time": event.get("time"),
        "scope": scope,
        "negative_controls": [],
        "source_event": event,
    }

    if ftype == "CLICK_POP":
        dtmf = _dtmf_negative_control(media, event, cfg)
        if dtmf:
            return {**base, "status": REJECTED_NEGATIVE_CONTROL, "reason_code": "DTMF_OVERLAP", "negative_controls": [dtmf]}
        boundary = _media_boundary_negative_control(event, cfg)
        if boundary:
            return {**base, "status": REJECTED_NEGATIVE_CONTROL, "reason_code": "MEDIA_BOUNDARY_TRANSIENT", "negative_controls": [boundary]}
        return {**base, "status": PROMOTED, "reason_code": "ACTIVE_MEDIA_MULTI_FEATURE_CLICK", "promoted_event": event}

    if ftype == "UNEXPECTED_SILENCE":
        matched, inconclusive = _rtp_silence_negative_control(media, event, cfg)
        if matched:
            return {**base, "status": REJECTED_NEGATIVE_CONTROL, "reason_code": "RTP_COUNTERPART_SILENCE", "negative_controls": [matched]}
        return {**base, "status": INCONCLUSIVE, **(inconclusive or {"reason_code": "COUNTERPART_ACTIVITY_NOT_PROVEN"})}

    return {**base, "status": INCONCLUSIVE, "reason_code": "UNSUPPORTED_CANDIDATE_TYPE"}


def _raw_pcm_candidate_decisions(media: dict) -> list[dict]:
    cfg = _cfg()
    calls = [c for c in (media.get("packet") or {}).get("calls", []) or [] if c.get("media_start_time") is not None and c.get("media_end_time") is not None]
    decisions: list[dict] = []
    pcm = media.get("pcm") or {}
    for stream in pcm.get("streams", []) or []:
        tap = (stream.get("tap") or {}).get("name")
        for session in stream.get("sessions", []) or []:
            session_start = float(session.get("start_time") or 0.0)
            session_index = session.get("session_index")
            for ev in session.get("click_pop_events", []) or []:
                when = session_start + float(ev.get("time_seconds") or 0.0)
                synthetic = {"type": "CLICK_POP", "time": when, "scope": {"pcm_tap": tap, "pcm_session_index": session_index}, "details": ev}
                dtmf = _dtmf_negative_control(media, synthetic, cfg)
                in_media = any(float(c["media_start_time"]) <= when <= float(c["media_end_time"]) for c in calls)
                if dtmf:
                    decisions.append({**decide_event(media, synthetic), "status": REJECTED_NEGATIVE_CONTROL, "reason_code": "DTMF_OVERLAP", "negative_controls": [dtmf], "raw_pcm_candidate": True})
                elif not in_media:
                    d = decide_event(media, synthetic)
                    d.update({"status": REJECTED_NEGATIVE_CONTROL, "reason_code": "OUTSIDE_ACTIVE_MEDIA_WINDOW", "raw_pcm_candidate": True})
                    decisions.append(d)
            for ev in session.get("silence_events", []) or []:
                start = session_start + float(ev.get("start_seconds") or 0.0)
                end = session_start + float(ev.get("end_seconds") or ev.get("start_seconds") or 0.0)
                overlaps = any(_overlap(start, end, float(c["media_start_time"]), float(c["media_end_time"])) > 0 for c in calls)
                if not overlaps:
                    synthetic = {"type": "UNEXPECTED_SILENCE", "time": start, "scope": {"pcm_tap": tap, "pcm_session_index": session_index}, "details": ev}
                    d = decide_event(media, synthetic)
                    d.update({"status": REJECTED_NEGATIVE_CONTROL, "reason_code": "OUTSIDE_ACTIVE_MEDIA_WINDOW", "raw_pcm_candidate": True})
                    decisions.append(d)
    return decisions


def apply_candidate_decisions(results: dict[str, dict | None]) -> dict[str, dict | None]:
    """Apply report/diagnosis-safe candidate decisions without deleting raw evidence.

    Raw PCM detector outputs are preserved under ``*_candidates`` for audit. Only
    promoted Media candidates remain in ``cross_layer_events`` where the Finding
    composer can turn them into user-visible findings.
    """
    media = results.get("media_intelligence")
    if not isinstance(media, dict):
        return results

    active = list(media.get("active_media_audio_events", []) or [])
    decisions = [decide_event(media, event) for event in active]
    decisions.extend(_raw_pcm_candidate_decisions(media))
    promoted = [d["promoted_event"] for d in decisions if d.get("status") == PROMOTED and d.get("promoted_event")]

    cross = [e for e in (media.get("cross_layer_events", []) or []) if e.get("type") not in {"CLICK_POP", "UNEXPECTED_SILENCE"}]
    cross.extend(promoted)
    media["cross_layer_events"] = cross
    media["candidate_decisions"] = decisions
    media["promoted_audio_events"] = promoted
    summary = media.setdefault("summary", {})
    summary["raw_audio_candidate_count"] = len(active)
    summary["promoted_audio_candidate_count"] = sum(1 for d in decisions if d.get("status") == PROMOTED)
    summary["rejected_audio_candidate_count"] = sum(1 for d in decisions if d.get("status") == REJECTED_NEGATIVE_CONTROL)
    summary["inconclusive_audio_candidate_count"] = sum(1 for d in decisions if d.get("status") == INCONCLUSIVE)
    summary["click_pop_count"] = sum(1 for e in promoted if e.get("type") == "CLICK_POP")
    summary["unexpected_silence_count"] = sum(1 for e in promoted if e.get("type") == "UNEXPECTED_SILENCE")

    # The standalone PCM Analyzer remains detector evidence, not a Finding source
    # for Click/Pop or Silence. Preserve raw candidates under explicit names.
    for key in ("pcm_intelligence",):
        pcm = results.get(key)
        if not isinstance(pcm, dict):
            continue
        for stream in pcm.get("streams", []) or []:
            for session in stream.get("sessions", []) or []:
                if "silence_events" in session:
                    session["silence_candidates"] = list(session.get("silence_events") or [])
                    session["silence_events"] = []
                if "click_pop_events" in session:
                    session["click_pop_candidates"] = list(session.get("click_pop_events") or [])
                    session["click_pop_events"] = []

    embedded_pcm = media.get("pcm")
    if isinstance(embedded_pcm, dict):
        for stream in embedded_pcm.get("streams", []) or []:
            for session in stream.get("sessions", []) or []:
                if "silence_events" in session:
                    session["silence_candidates"] = list(session.get("silence_events") or [])
                if "click_pop_events" in session:
                    session["click_pop_candidates"] = list(session.get("click_pop_events") or [])

    return results
