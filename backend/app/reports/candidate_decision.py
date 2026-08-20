from __future__ import annotations

from copy import deepcopy

from app.analyzers.audio.candidate_decision import (
    INCONCLUSIVE,
    PROMOTED,
    SUPPRESSED,
    decide_click_pop,
    decide_silence,
)


_CANDIDATE_TYPES = {"CLICK_POP", "UNEXPECTED_SILENCE"}


def _pcm_session(media: dict, scope: dict) -> dict | None:
    pcm = media.get("pcm") or {}
    tap_name = scope.get("pcm_tap")
    session_index = scope.get("pcm_session_index")
    for stream in pcm.get("streams", []) or []:
        if (stream.get("tap") or {}).get("name") != tap_name:
            continue
        for session in stream.get("sessions", []) or []:
            if session.get("session_index") == session_index:
                return session
    return None


def _absolute_dtmf_intervals(media: dict, scope: dict) -> list[dict]:
    session = _pcm_session(media, scope)
    if not session:
        return []
    base = float(session.get("start_time") or 0.0)
    out = []
    for event in session.get("dtmf_events", []) or []:
        start = base + float(event.get("start_seconds") or 0.0)
        end = base + float(event.get("end_seconds") if event.get("end_seconds") is not None else event.get("start_seconds") or 0.0)
        out.append({"digit": event.get("digit"), "start": start, "end": end, "confidence": event.get("confidence")})
    return out


def _best_pcm_rtp_correlation(media: dict, scope: dict) -> tuple[str | None, float | None, str | None, float]:
    tap = scope.get("pcm_tap")
    session_index = scope.get("pcm_session_index")
    best = None
    for event in media.get("correlations", []) or []:
        if event.get("type") != "PCM_RTP_CORRELATION":
            continue
        details = event.get("details") or {}
        if details.get("pcm_tap") != tap or details.get("pcm_session_index") != session_index:
            continue
        corr = details.get("correlation") or {}
        score = abs(float(corr.get("absolute_correlation") or 0.0))
        if best is None or score > best[0]:
            best = (score, details.get("rtp_stream_id"), corr.get("quality"), float(corr.get("lag_ms") or 0.0))
    if best is None:
        return None, None, None, 0.0
    return best[1], best[0], best[2], best[3]


def _rtp_track(media: dict, stream_id: str | None) -> dict | None:
    if not stream_id:
        return None
    return next((x for x in media.get("rtp_audio_tracks", []) or [] if x.get("stream_id") == stream_id), None)


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def _matched_rtp_silence_metrics(track: dict | None, absolute_start: float, absolute_end: float) -> dict | None:
    if not track:
        return None
    base = float(track.get("start_time") or 0.0)
    best = None
    for event in track.get("silence_events", []) or []:
        start = base + float(event.get("start_seconds") or 0.0)
        end = base + float(event.get("end_seconds") if event.get("end_seconds") is not None else event.get("start_seconds") or 0.0)
        overlap = _overlap(absolute_start, absolute_end, start, end)
        if overlap <= 0:
            continue
        if best is None or overlap > best[0]:
            best = (overlap, event, start, end)
    if best is None:
        return None
    _, event, start, end = best
    pre = float(event.get("pre_context_dbfs") if event.get("pre_context_dbfs") is not None else -120.0)
    post = float(event.get("post_context_dbfs") if event.get("post_context_dbfs") is not None else -120.0)
    return {
        "absolute_start_time": start,
        "absolute_end_time": end,
        "event_rms_dbfs": float(event.get("median_dbfs") if event.get("median_dbfs") is not None else -120.0),
        "pre_context_rms_dbfs": pre,
        "post_context_rms_dbfs": post,
        "context_peak_dbfs": max(pre, post),
        "source": "RTP_UNEXPECTED_SILENCE_DETECTOR",
    }


def _legacy_click_decision(media: dict, event: dict) -> dict:
    scope = dict(event.get("scope") or {})
    details = dict(event.get("details") or {})
    window = scope.get("active_media_window") or {}
    absolute_time = float(event.get("time") or details.get("absolute_time") or 0.0)
    start = float(window.get("start_time") if window.get("start_time") is not None else absolute_time)
    end = float(window.get("end_time") if window.get("end_time") is not None else absolute_time)
    return decide_click_pop(
        details,
        absolute_time=absolute_time,
        scope=scope,
        dtmf_intervals=_absolute_dtmf_intervals(media, scope),
        media_start=start,
        media_end=end,
    )


def _legacy_silence_decision(media: dict, event: dict) -> dict:
    scope = dict(event.get("scope") or {})
    details = dict(event.get("details") or {})
    window = scope.get("active_media_window") or {}
    base = float(window.get("start_time") if window.get("start_time") is not None else event.get("time") or 0.0)
    absolute_start = float(details.get("absolute_start_time") if details.get("absolute_start_time") is not None else event.get("time") or base)
    if details.get("end_seconds") is not None:
        absolute_end = base + float(details.get("end_seconds") or 0.0)
    else:
        absolute_end = absolute_start + float(details.get("duration_ms") or 0.0) / 1000.0
    stream_id, correlation, quality, lag_ms = _best_pcm_rtp_correlation(media, scope)
    track = _rtp_track(media, stream_id)
    # correlate_tracks defines positive lag as PCM(a) delayed relative to RTP(b),
    # therefore the corresponding RTP source window is PCM window - lag.
    aligned_start = absolute_start - lag_ms / 1000.0
    aligned_end = absolute_end - lag_ms / 1000.0
    metrics = _matched_rtp_silence_metrics(track, aligned_start, aligned_end)
    if metrics is not None:
        metrics["pcm_absolute_start_time"] = absolute_start
        metrics["pcm_absolute_end_time"] = absolute_end
        metrics["correlation_lag_ms"] = lag_ms
        metrics["alignment_rule"] = "rtp_window = pcm_window - correlation_lag"
    decision = decide_silence(
        details,
        absolute_start=absolute_start,
        absolute_end=absolute_end,
        scope=scope,
        counterpart_stream_id=stream_id,
        counterpart_correlation=correlation,
        counterpart_metrics=metrics,
    )
    evidence = decision.setdefault("positive_evidence", {})
    evidence["correlation_quality"] = quality
    evidence["correlation_lag_ms"] = lag_ms
    evidence["counterpart_aligned_start_time"] = aligned_start
    evidence["counterpart_aligned_end_time"] = aligned_end
    if stream_id and correlation is not None and metrics is None and decision.get("status") == INCONCLUSIVE:
        decision["reason_code"] = "RTP_COUNTERPART_WINDOW_ACTIVITY_NOT_DIRECTLY_MEASURED"
    return decision


def resolve_candidate_decisions(media: dict | None) -> dict:
    """Resolve report-facing audio candidates without rewriting Analyzer raw evidence.

    Raw Analyzer candidates remain intact for drill-down. The report consumes only
    candidates explicitly marked PROMOTED by this deterministic policy. Legacy Media
    results without CandidateDecision are re-evaluated fail-closed.
    """
    if not media:
        return {"decisions": [], "promoted_events": [], "suppressed": 0, "inconclusive": 0, "promoted": 0}
    decisions = []
    promoted_events = []
    for event in media.get("cross_layer_events", []) or []:
        ftype = str(event.get("type") or "")
        if ftype not in _CANDIDATE_TYPES:
            continue
        details = event.get("details") or {}
        existing = details.get("candidate_decision") if isinstance(details, dict) else None
        if isinstance(existing, dict) and existing.get("status") in {PROMOTED, SUPPRESSED, INCONCLUSIVE}:
            decision = deepcopy(existing)
        elif ftype == "CLICK_POP":
            decision = _legacy_click_decision(media, event)
        else:
            decision = _legacy_silence_decision(media, event)
        decisions.append(decision)
        if decision.get("status") == PROMOTED:
            promoted = deepcopy(event)
            promoted_details = dict(promoted.get("details") or {})
            promoted_details["candidate_decision"] = decision
            if ftype == "CLICK_POP":
                promoted_details["interpretation"] = "Click/Pop 候选已通过 DTMF、媒体边界和置信度 Negative Control，可作为活跃媒体异常候选进入报告；仍不等同于物理根因。"
            else:
                promoted_details["interpretation"] = "PCM 静音候选已通过按 correlation lag 对齐的跨层对照，关联 RTP 方向在对应源窗口仍保持活动证据，支持存在媒体链路静音不一致；仍不等同于物理根因。"
            promoted["details"] = promoted_details
            promoted_events.append(promoted)
    return {
        "decisions": decisions,
        "promoted_events": promoted_events,
        "promoted": sum(1 for x in decisions if x.get("status") == PROMOTED),
        "suppressed": sum(1 for x in decisions if x.get("status") == SUPPRESSED),
        "inconclusive": sum(1 for x in decisions if x.get("status") == INCONCLUSIVE),
    }


def decision_summary(media: dict | None) -> dict:
    resolved = resolve_candidate_decisions(media)
    by_reason: dict[str, int] = {}
    by_type: dict[str, dict[str, int]] = {}
    for decision in resolved["decisions"]:
        reason = str(decision.get("reason_code") or "UNKNOWN")
        by_reason[reason] = by_reason.get(reason, 0) + 1
        kind = str(decision.get("candidate_type") or "UNKNOWN")
        group = by_type.setdefault(kind, {PROMOTED: 0, SUPPRESSED: 0, INCONCLUSIVE: 0})
        status = str(decision.get("status") or INCONCLUSIVE)
        group[status] = group.get(status, 0) + 1
    return {
        "policy_version": "candidate-decision-v1",
        "candidate_count": len(resolved["decisions"]),
        "promoted": resolved["promoted"],
        "suppressed": resolved["suppressed"],
        "inconclusive": resolved["inconclusive"],
        "by_type": by_type,
        "by_reason": by_reason,
        "decisions": resolved["decisions"],
    }
