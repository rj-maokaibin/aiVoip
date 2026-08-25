from __future__ import annotations

from typing import Any


def _float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def periodic_measurement(metrics: dict | None, *, source: str = "pcm_rx") -> dict:
    """Project Analyzer periodic facts into a renderer-safe measurement contract.

    This function does not re-run analysis and does not infer a Root Cause. It only
    copies numeric facts already present in PERIODIC_METRICS_JSON.
    """
    event = metrics or {}
    details = event.get("details") or {}
    result = details.get(source) or {}
    representative = result.get("representative") or {}
    estimated = result.get("estimated_period") or {}
    comb = result.get("comb") or {}
    members = list(comb.get("members") or [])
    hits = [m for m in members if bool(m.get("hit"))]
    harmonics = [
        _float(m.get("peak_hz")) if _float(m.get("peak_hz")) is not None else _float(m.get("target_hz"))
        for m in hits
    ]
    harmonics = [x for x in harmonics if x is not None]
    return {
        "source": source,
        "level": result.get("level") or details.get("level"),
        "period_ms": _float(estimated.get("period_ms")),
        "frequency_hz": _float(estimated.get("frequency_hz")),
        "period_correlation": _float(estimated.get("correlation")),
        "rms_dbfs": _float(representative.get("rms_dbfs")),
        "periodicity_score": _float(representative.get("periodicity_score")),
        "autocorrelation": dict(representative.get("autocorrelation") or {}),
        "representative_start_seconds": _float(representative.get("start_seconds")),
        "representative_absolute_start_time": _float(representative.get("absolute_start_time")),
        "representative_duration_seconds": _float(representative.get("duration_seconds")),
        "comb_detected": bool(comb.get("detected")),
        "comb_pattern": comb.get("pattern"),
        "comb_base_hz": _float(comb.get("base_hz")),
        "comb_spacing_hz": _float(comb.get("spacing_hz")),
        "comb_hit_count": int(comb.get("hit_count") or 0),
        "comb_target_count": int(comb.get("target_count") or 0),
        "comb_mean_prominence_db": _float(comb.get("mean_prominence_db")),
        "harmonics_hz": harmonics,
        "analysis_window": dict(result.get("analysis_window") or {}),
        "canonical_interpretation": result.get("interpretation") or details.get("interpretation"),
        "canonical_evidence_boundary": details.get("evidence_boundary"),
    }


def merge_visual_measurement(renderer_measurement: dict | None, periodic: dict | None, **extra) -> dict:
    out = dict(renderer_measurement or {})
    if periodic:
        out["periodic"] = dict(periodic)
    for key, value in extra.items():
        if value is not None:
            out[key] = value
    return out
