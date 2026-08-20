from __future__ import annotations

from .candidate_gate_core import *  # noqa: F401,F403
from .candidate_gate_core import (
    _candidate_id,
    _dtmf_intervals,
    _gate_config,
    _overlaps,
    build_diagnostic_candidates as _core_build_diagnostic_candidates,
)


OUTSIDE_ACTIVE_MEDIA_WINDOW = "OUTSIDE_ACTIVE_MEDIA_WINDOW"


def _active_media_windows(media: dict | None) -> list[dict]:
    packet = (media or {}).get("packet") or {}
    out = []
    for call in packet.get("calls", []) or []:
        start = call.get("media_start_time")
        end = call.get("media_end_time")
        if start is None or end is None:
            continue
        try:
            a = float(start); b = float(end)
        except (TypeError, ValueError):
            continue
        if b >= a:
            out.append({"call_id": call.get("call_id"), "start": a, "end": b})
    return out


def _outside_all_active_windows(start: float, end: float, windows: list[dict]) -> bool:
    return bool(windows) and not any(_overlaps(start, end, x["start"], x["end"]) for x in windows)


def _raw_outside_active_candidates(*, pcm: dict | None, media: dict | None) -> list[dict]:
    """Preserve an auditable suppression reason for raw detector events outside a Call.

    The core Candidate Gate intentionally accepts only active-media-scoped events.
    This audit layer adds only raw PCM events that are provably outside every
    reconstructed Call media window. Raw events inside a Call are not duplicated;
    they must be represented by the active-media detector/candidate path.
    """
    windows = _active_media_windows(media)
    if not windows:
        return []
    cfg = _gate_config()
    guard = float(cfg.get("click_dtmf_guard_ms") or 25.0) / 1000.0
    out: list[dict] = []
    for stream in (pcm or {}).get("streams", []) or []:
        tap = stream.get("tap") or {}
        tap_name = str(tap.get("name") or "pcm")
        direction = str(tap.get("direction") or "").upper() or None
        for session in stream.get("sessions", []) or []:
            if session.get("start_time") is None:
                continue
            try:
                base = float(session["start_time"])
                session_index = int(session.get("session_index") or 0)
            except (TypeError, ValueError):
                continue
            dtmf = _dtmf_intervals(session, guard_seconds=guard)
            scope = {
                "layer": tap_name.upper(),
                "pcm_tap": tap_name,
                "pcm_direction": direction,
                "pcm_session_index": session_index,
                "direction": direction,
            }

            for index, raw in enumerate(session.get("click_pop_events", []) or []):
                try:
                    when = base + float(raw.get("time_seconds") or 0.0)
                except (TypeError, ValueError):
                    continue
                if not _outside_all_active_windows(when, when, windows):
                    continue
                event = {"type": "CLICK_POP", "time": when, "scope": scope, "details": dict(raw)}
                reasons = [OUTSIDE_ACTIVE_MEDIA_WINDOW]
                context = {"active_media_windows": windows, "dtmf_guard_ms": round(guard * 1000.0, 3)}
                overlap = next((x for x in dtmf if x["start"] <= when <= x["end"]), None)
                if overlap:
                    reasons.append(DTMF_TRANSIENT_OVERLAP)
                    context["overlapping_dtmf"] = overlap
                metrics = dict(raw)
                metrics.update({"candidate_decision": CandidateDecision.SUPPRESS.value, "candidate_reason_codes": reasons})
                out.append({
                    "candidate_id": _candidate_id(event),
                    "type": "CLICK_POP",
                    "decision": CandidateDecision.SUPPRESS.value,
                    "reason_codes": reasons,
                    "severity": "MEDIUM",
                    "evidence_level": "L3",
                    "time_range": {"start": when, "end": when, "representative": when},
                    "scope": scope,
                    "metrics": metrics,
                    "context": context,
                    "source_event_ref": {"source": "pcm.raw.click_pop_events", "index": index},
                })

            for index, raw in enumerate(session.get("silence_events", []) or []):
                try:
                    start = base + float(raw.get("start_seconds") or 0.0)
                    end = base + float(raw.get("end_seconds") if raw.get("end_seconds") is not None else raw.get("start_seconds") or 0.0)
                except (TypeError, ValueError):
                    continue
                if not _outside_all_active_windows(start, end, windows):
                    continue
                details = dict(raw)
                event = {"type": "UNEXPECTED_SILENCE", "time": start, "scope": scope, "details": details}
                reasons = [OUTSIDE_ACTIVE_MEDIA_WINDOW]
                metrics = dict(raw)
                metrics.update({"candidate_decision": CandidateDecision.SUPPRESS.value, "candidate_reason_codes": reasons})
                out.append({
                    "candidate_id": _candidate_id(event),
                    "type": "UNEXPECTED_SILENCE",
                    "decision": CandidateDecision.SUPPRESS.value,
                    "reason_codes": reasons,
                    "severity": "MEDIUM",
                    "evidence_level": "L3",
                    "time_range": {"start": start, "end": end, "representative": start},
                    "scope": scope,
                    "metrics": metrics,
                    "context": {"active_media_windows": windows},
                    "source_event_ref": {"source": "pcm.raw.silence_events", "index": index},
                })
    return out


def build_diagnostic_candidates(*, pcm: dict | None, media: dict | None) -> list[dict]:
    """Return context-gated candidates plus explicit out-of-Call suppressions."""
    effective_pcm = pcm or (media or {}).get("pcm") or {}
    candidates = list(_core_build_diagnostic_candidates(pcm=effective_pcm, media=media))
    candidates.extend(_raw_outside_active_candidates(pcm=effective_pcm, media=media))
    dedup: dict[str, dict] = {}
    for candidate in candidates:
        dedup.setdefault(str(candidate.get("candidate_id")), candidate)
    out = list(dedup.values())
    out.sort(key=lambda x: (
        (x.get("time_range") or {}).get("representative") is None,
        (x.get("time_range") or {}).get("representative") or 0.0,
        str(x.get("candidate_id") or ""),
    ))
    return out
