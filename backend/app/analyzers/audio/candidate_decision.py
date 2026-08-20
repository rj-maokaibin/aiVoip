from __future__ import annotations

import hashlib
import json
import math
from typing import Any

import numpy as np

from app.analyzers.profile import get_default_analyzer_profile


PROMOTED = "PROMOTED"
SUPPRESSED = "SUPPRESSED"
INCONCLUSIVE = "INCONCLUSIVE"
CANDIDATE_DECISION_POLICY_VERSION = "candidate-decision-v1"


_DEFAULT_CONFIG = {
    "click_pop": {
        "dtmf_guard_ms": 80.0,
        "media_boundary_guard_ms": 120.0,
        "min_promote_confidence": 0.65,
    },
    "silence": {
        "min_counterpart_correlation": 0.80,
        "counterpart_quiet_dbfs": -52.0,
        "counterpart_active_dbfs": -42.0,
        "context_seconds": 0.12,
        "source_drop_db": 6.0,
    },
}


def _config(section: str) -> dict[str, Any]:
    base = dict(_DEFAULT_CONFIG[section])
    profile = get_default_analyzer_profile()
    extra = profile.config.get("candidate_decision") or {}
    if isinstance(extra, dict) and isinstance(extra.get(section), dict):
        base.update(extra[section])
    return base


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _candidate_id(candidate_type: str, *, scope: dict | None, start: float | None, end: float | None, raw: dict | None) -> str:
    material = _canonical({
        "type": candidate_type,
        "scope": scope or {},
        "start": start,
        "end": end,
        "raw": raw or {},
        "policy": CANDIDATE_DECISION_POLICY_VERSION,
    })
    return "cand-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:20]


def _decision(
    candidate_type: str,
    status: str,
    reason_code: str,
    *,
    scope: dict | None,
    start: float | None,
    end: float | None,
    raw: dict | None,
    negative_controls: list[dict] | None = None,
    positive_evidence: dict | None = None,
) -> dict:
    return {
        "candidate_id": _candidate_id(candidate_type, scope=scope, start=start, end=end, raw=raw),
        "candidate_type": candidate_type,
        "status": status,
        "reason_code": reason_code,
        "policy_version": CANDIDATE_DECISION_POLICY_VERSION,
        "time_range": {"start": start, "end": end, "representative": start},
        "scope": scope or {},
        "negative_controls": negative_controls or [],
        "positive_evidence": positive_evidence or {},
        "raw_candidate": raw or {},
    }


def _overlaps(t: float, start: float, end: float, guard_seconds: float = 0.0) -> bool:
    return (start - guard_seconds) <= t <= (end + guard_seconds)


def decide_raw_click_pop(candidate: dict, dtmf_events: list[dict], *, scope: dict | None = None) -> dict:
    """Classify a raw PCM click/pop candidate before Call/cross-layer context exists.

    A raw PCM detector is intentionally not allowed to promote a user-visible Finding.
    It can only suppress a known normal transient (currently DTMF) or stay inconclusive
    until Media Intelligence supplies active-media and cross-layer context.
    """
    cfg = _config("click_pop")
    t = float(candidate.get("time_seconds") or 0.0)
    guard = float(cfg["dtmf_guard_ms"]) / 1000.0
    for event in dtmf_events or []:
        start = float(event.get("start_seconds") or 0.0)
        end = float(event.get("end_seconds") if event.get("end_seconds") is not None else start)
        if _overlaps(t, start, end, guard):
            return _decision(
                "CLICK_POP", SUPPRESSED, "NEGCTRL_DTMF_TRANSIENT",
                scope=scope, start=t, end=t, raw=candidate,
                negative_controls=[{
                    "type": "DTMF_TRANSIENT",
                    "digit": event.get("digit"),
                    "dtmf_start_seconds": start,
                    "dtmf_end_seconds": end,
                    "guard_ms": float(cfg["dtmf_guard_ms"]),
                }],
            )
    return _decision(
        "CLICK_POP", INCONCLUSIVE, "RAW_PCM_REQUIRES_ACTIVE_MEDIA_CONTEXT",
        scope=scope, start=t, end=t, raw=candidate,
    )


def decide_raw_silence(candidate: dict, *, scope: dict | None = None) -> dict:
    start = float(candidate.get("start_seconds") or 0.0)
    end = float(candidate.get("end_seconds") if candidate.get("end_seconds") is not None else start)
    return _decision(
        "UNEXPECTED_SILENCE", INCONCLUSIVE, "RAW_PCM_REQUIRES_CROSS_LAYER_COUNTERPART",
        scope=scope, start=start, end=end, raw=candidate,
    )


def decide_click_pop(
    candidate: dict,
    *,
    absolute_time: float,
    scope: dict,
    dtmf_intervals: list[dict] | None,
    media_start: float,
    media_end: float,
) -> dict:
    """Apply deterministic negative controls to an active-media click/pop candidate."""
    cfg = _config("click_pop")
    dtmf_guard = float(cfg["dtmf_guard_ms"]) / 1000.0
    boundary_guard = float(cfg["media_boundary_guard_ms"]) / 1000.0

    for event in dtmf_intervals or []:
        start = float(event.get("start") or 0.0)
        end = float(event.get("end") if event.get("end") is not None else start)
        if _overlaps(absolute_time, start, end, dtmf_guard):
            return _decision(
                "CLICK_POP", SUPPRESSED, "NEGCTRL_DTMF_TRANSIENT",
                scope=scope, start=absolute_time, end=absolute_time, raw=candidate,
                negative_controls=[{
                    "type": "DTMF_TRANSIENT",
                    "digit": event.get("digit"),
                    "absolute_start_time": start,
                    "absolute_end_time": end,
                    "guard_ms": float(cfg["dtmf_guard_ms"]),
                }],
            )

    if absolute_time - media_start <= boundary_guard or media_end - absolute_time <= boundary_guard:
        return _decision(
            "CLICK_POP", SUPPRESSED, "NEGCTRL_MEDIA_BOUNDARY_TRANSIENT",
            scope=scope, start=absolute_time, end=absolute_time, raw=candidate,
            negative_controls=[{
                "type": "MEDIA_BOUNDARY_TRANSIENT",
                "media_start_time": media_start,
                "media_end_time": media_end,
                "guard_ms": float(cfg["media_boundary_guard_ms"]),
            }],
        )

    confidence = float(candidate.get("confidence") or 0.0)
    if confidence < float(cfg["min_promote_confidence"]):
        return _decision(
            "CLICK_POP", INCONCLUSIVE, "CLICK_POP_CONFIDENCE_BELOW_PROMOTION_GATE",
            scope=scope, start=absolute_time, end=absolute_time, raw=candidate,
            positive_evidence={"confidence": confidence, "required_confidence": float(cfg["min_promote_confidence"])},
        )

    return _decision(
        "CLICK_POP", PROMOTED, "CLICK_POP_NEGATIVE_CONTROLS_CLEARED",
        scope=scope, start=absolute_time, end=absolute_time, raw=candidate,
        positive_evidence={
            "confidence": confidence,
            "jump": candidate.get("jump"),
            "energy_rise_db": candidate.get("energy_rise_db"),
            "highband_energy_ratio": candidate.get("highband_energy_ratio"),
        },
    )


def _dbfs_rms(samples: np.ndarray) -> float:
    if samples.size == 0:
        return -120.0
    x = samples.astype(np.float64, copy=False)
    rms = float(np.sqrt(np.mean(x * x)))
    return -120.0 if rms <= 0 else 20.0 * math.log10(rms / 32768.0)


def audio_window_metrics(
    samples: np.ndarray,
    sample_rate: int,
    track_start_time: float,
    absolute_start: float,
    absolute_end: float,
    *,
    context_seconds: float | None = None,
) -> dict:
    """Measure source energy in an already alignment-resolved absolute window plus short context."""
    cfg = _config("silence")
    context = float(context_seconds if context_seconds is not None else cfg["context_seconds"])
    start = max(track_start_time, absolute_start)
    end = max(start, absolute_end)

    def cut(a: float, b: float) -> np.ndarray:
        ia = max(0, int(round((a - track_start_time) * sample_rate)))
        ib = min(samples.size, int(round((b - track_start_time) * sample_rate)))
        return samples[ia:ib] if ib > ia else np.zeros(0, dtype=samples.dtype)

    event = cut(start, end)
    pre = cut(max(track_start_time, start - context), start)
    post = cut(end, end + context)
    event_dbfs = _dbfs_rms(event)
    pre_dbfs = _dbfs_rms(pre)
    post_dbfs = _dbfs_rms(post)
    return {
        "absolute_start_time": absolute_start,
        "absolute_end_time": absolute_end,
        "event_rms_dbfs": round(event_dbfs, 3),
        "pre_context_rms_dbfs": round(pre_dbfs, 3),
        "post_context_rms_dbfs": round(post_dbfs, 3),
        "context_peak_dbfs": round(max(pre_dbfs, post_dbfs), 3),
        "sample_count": int(event.size),
    }


def decide_silence(
    candidate: dict,
    *,
    absolute_start: float,
    absolute_end: float,
    scope: dict,
    counterpart_stream_id: str | None,
    counterpart_correlation: float | None,
    counterpart_metrics: dict | None,
) -> dict:
    """Promote silence only when a highly correlated RTP counterpart remains active.

    If the corresponding lag-aligned RTP source window is also quiet, the PCM
    silence is expected source/content silence and is suppressed. If no trustworthy
    counterpart exists, the candidate stays inconclusive rather than becoming a
    MEDIUM finding.
    """
    cfg = _config("silence")
    min_corr = float(cfg["min_counterpart_correlation"])
    corr = abs(float(counterpart_correlation or 0.0))
    if not counterpart_stream_id or counterpart_metrics is None or corr < min_corr:
        return _decision(
            "UNEXPECTED_SILENCE", INCONCLUSIVE, "SILENCE_COUNTERPART_RTP_UNAVAILABLE_OR_WEAK",
            scope=scope, start=absolute_start, end=absolute_end, raw=candidate,
            positive_evidence={
                "counterpart_stream_id": counterpart_stream_id,
                "absolute_correlation": round(corr, 6),
                "required_correlation": min_corr,
            },
        )

    event_dbfs = float(counterpart_metrics.get("event_rms_dbfs", -120.0))
    context_peak = float(counterpart_metrics.get("context_peak_dbfs", -120.0))
    quiet_threshold = float(cfg["counterpart_quiet_dbfs"])
    active_threshold = float(cfg["counterpart_active_dbfs"])
    source_drop_db = float(cfg["source_drop_db"])
    source_quiet = event_dbfs <= quiet_threshold or (
        context_peak > -119.0 and event_dbfs <= active_threshold and (context_peak - event_dbfs) >= source_drop_db
    )

    evidence = {
        "counterpart_stream_id": counterpart_stream_id,
        "absolute_correlation": round(corr, 6),
        "counterpart_window": counterpart_metrics,
        "quiet_threshold_dbfs": quiet_threshold,
        "active_threshold_dbfs": active_threshold,
        "source_drop_db": source_drop_db,
    }
    if source_quiet:
        return _decision(
            "UNEXPECTED_SILENCE", SUPPRESSED, "NEGCTRL_MATCHED_RTP_SOURCE_SILENCE",
            scope=scope, start=absolute_start, end=absolute_end, raw=candidate,
            negative_controls=[{
                "type": "MATCHED_RTP_SOURCE_SILENCE",
                "rtp_stream_id": counterpart_stream_id,
                "event_rms_dbfs": event_dbfs,
                "context_peak_dbfs": context_peak,
            }],
            positive_evidence=evidence,
        )

    if event_dbfs >= active_threshold:
        return _decision(
            "UNEXPECTED_SILENCE", PROMOTED, "CROSS_LAYER_SILENCE_MISMATCH_CONFIRMED",
            scope=scope, start=absolute_start, end=absolute_end, raw=candidate,
            positive_evidence=evidence,
        )

    return _decision(
        "UNEXPECTED_SILENCE", INCONCLUSIVE, "RTP_COUNTERPART_ACTIVITY_AMBIGUOUS",
        scope=scope, start=absolute_start, end=absolute_end, raw=candidate,
        positive_evidence=evidence,
    )
