from __future__ import annotations

import math

import numpy as np

from app.analyzers.profile import get_default_analyzer_profile


def _dbfs_rms(x: np.ndarray) -> float:
    if x.size == 0:
        return -120.0
    y = x.astype(np.float64, copy=False)
    rms = float(np.sqrt(np.mean(y * y)))
    return -120.0 if rms <= 0 else 20.0 * math.log10(rms / 32768.0)


def _db_ratio(a: float, b: float) -> float:
    return 20.0 * math.log10((a + 1e-12) / (b + 1e-12))


def detect_unexpected_silence(
    samples: np.ndarray,
    sample_rate: int,
    *,
    min_duration_ms: int | None = None,
    frame_ms: int | None = None,
    active_regions: list[tuple[float, float]] | None = None,
) -> list[dict]:
    """Detect unexpected silence inside active media only.

    Critical thresholds come from the versioned AnalyzerProfile. Explicit keyword values are
    test/experiment overrides and do not mutate the profile. Silence without voiced context on
    both sides is not promoted to an interruption finding.
    """
    cfg = get_default_analyzer_profile().section("silence")
    min_duration_ms = int(min_duration_ms if min_duration_ms is not None else cfg["min_duration_ms"])
    frame_ms = int(frame_ms if frame_ms is not None else cfg["frame_ms"])
    floor_pct = float(cfg["noise_floor_percentile"])
    speech_pct = float(cfg["speech_percentile"])
    threshold_min = float(cfg["threshold_min_dbfs"])
    threshold_max = float(cfg["threshold_max_dbfs"])
    floor_margin = float(cfg["noise_floor_margin_db"])
    speech_margin = float(cfg["speech_margin_db"])
    context_ms = float(cfg["context_ms"])
    context_margin = float(cfg["context_voice_margin_db"])

    x = samples.astype(np.float64, copy=False)
    frame = max(1, round(sample_rate * frame_ms / 1000.0))
    if x.size < frame * 3:
        return []
    starts = np.arange(0, x.size - frame + 1, frame, dtype=int)
    levels = np.array([_dbfs_rms(x[s:s + frame]) for s in starts], dtype=float)
    finite = levels[np.isfinite(levels)]
    if finite.size == 0:
        return []
    floor = float(np.percentile(finite, floor_pct))
    speech = float(np.percentile(finite, speech_pct))
    threshold = min(threshold_max, max(threshold_min, min(floor + floor_margin, speech - speech_margin)))

    active = np.zeros(len(starts), dtype=bool)
    if active_regions:
        for a, b in active_regions:
            ia = max(0, int(math.floor(a * 1000 / frame_ms)))
            ib = min(len(active), int(math.ceil(b * 1000 / frame_ms)))
            if ib > ia:
                active[ia:ib] = True
    else:
        active[:] = True

    quiet = (levels <= threshold) & active
    min_frames = max(1, math.ceil(min_duration_ms / frame_ms))
    context_frames = max(2, math.ceil(context_ms / frame_ms))
    events = []
    begin = None
    for i, q in enumerate(np.r_[quiet, False]):
        if q and begin is None:
            begin = i
        elif not q and begin is not None:
            end = i
            n = end - begin
            if n >= min_frames:
                pre = levels[max(0, begin - context_frames):begin]
                post = levels[end:min(len(levels), end + context_frames)]
                pre_active = active[max(0, begin - context_frames):begin]
                post_active = active[end:min(len(active), end + context_frames)]
                pre_v = float(np.max(pre[pre_active])) if pre.size and np.any(pre_active) else -120.0
                post_v = float(np.max(post[post_active])) if post.size and np.any(post_active) else -120.0
                if pre_v >= threshold + context_margin and post_v >= threshold + context_margin:
                    events.append({
                        "type": "UNEXPECTED_SILENCE",
                        "start_seconds": round(begin * frame_ms / 1000.0, 6),
                        "end_seconds": round(end * frame_ms / 1000.0, 6),
                        "duration_ms": round(n * frame_ms, 3),
                        "threshold_dbfs": round(threshold, 3),
                        "median_dbfs": round(float(np.median(levels[begin:end])), 3),
                        "pre_context_dbfs": round(pre_v, 3),
                        "post_context_dbfs": round(post_v, 3),
                        "evidence_level": "L2",
                    })
            begin = None
    return events


def detect_click_pop_robust(
    samples: np.ndarray,
    sample_rate: int,
    *,
    min_jump: float | None = None,
    min_energy_rise_db: float | None = None,
    min_highband_ratio: float | None = None,
) -> list[dict]:
    """Multi-feature click/pop detector backed by the versioned AnalyzerProfile."""
    cfg = get_default_analyzer_profile().section("click_pop")
    confidence_cfg = cfg["confidence"]
    min_jump = float(min_jump if min_jump is not None else cfg["min_jump"])
    min_energy_rise_db = float(min_energy_rise_db if min_energy_rise_db is not None else cfg["min_energy_rise_db"])
    min_highband_ratio = float(min_highband_ratio if min_highband_ratio is not None else cfg["min_highband_ratio"])
    mad_multiplier = float(cfg["mad_multiplier"])
    merge_ms = float(cfg["merge_ms"])
    short_ms = float(cfg["short_window_ms"])
    guard_ms = float(cfg["guard_ms"])
    highband_min_hz = float(cfg["highband_min_hz"])
    max_events = int(cfg["max_events"])

    x = samples.astype(np.float64, copy=False)
    if x.size < max(64, round(sample_rate * 0.08)):
        return []
    d = np.abs(np.diff(x))
    med = float(np.median(d))
    mad = float(np.median(np.abs(d - med))) + 1e-9
    threshold = max(min_jump, med + mad_multiplier * mad)
    cand = np.flatnonzero(d >= threshold)
    merge = max(1, round(sample_rate * merge_ms / 1000.0))
    short = max(8, round(sample_rate * short_ms / 1000.0))
    guard = max(short + 1, round(sample_rate * guard_ms / 1000.0))
    events = []
    last = -10**9
    for idx in cand:
        if idx - last <= merge:
            last = idx
            continue
        c0 = max(0, idx - short // 2)
        c1 = min(x.size, idx + short // 2)
        local = x[c0:c1]
        p0, p1 = max(0, c0 - guard), c0
        q0, q1 = c1, min(x.size, c1 + guard)
        baseline = np.r_[x[p0:p1], x[q0:q1]]
        if local.size < 8 or baseline.size < 16:
            last = idx
            continue
        lr = float(np.sqrt(np.mean(local * local)))
        br = float(np.sqrt(np.mean(baseline * baseline))) + 1e-9
        rise = _db_ratio(lr, br)
        y = (local - float(np.mean(local))) * np.hanning(local.size)
        spec = np.abs(np.fft.rfft(y)) ** 2
        freqs = np.fft.rfftfreq(local.size, 1.0 / sample_rate)
        total = float(np.sum(spec)) + 1e-12
        high = float(np.sum(spec[freqs >= highband_min_hz])) / total if total else 0.0
        if rise < min_energy_rise_db or high < min_highband_ratio:
            last = idx
            continue
        confidence = min(
            1.0,
            float(confidence_cfg["base"])
            + float(confidence_cfg["jump_weight"]) * min(1.0, (float(d[idx]) - threshold) / (threshold + 1e-9))
            + float(confidence_cfg["rise_weight"]) * min(1.0, rise / float(confidence_cfg["rise_full_scale_db"]))
            + float(confidence_cfg["highband_weight"]) * min(1.0, high / float(confidence_cfg["highband_full_scale_ratio"])),
        )
        events.append({
            "type": "CLICK_POP",
            "time_seconds": round((idx + 1) / sample_rate, 6),
            "jump": round(float(d[idx]), 3),
            "jump_threshold": round(threshold, 3),
            "energy_rise_db": round(rise, 3),
            "highband_energy_ratio": round(high, 6),
            "confidence": round(confidence, 6),
            "classification": "CLICK_POP_CANDIDATE",
            "evidence_level": "L3",
        })
        last = idx
    return events[:max_events]


def analyze_echo_path(
    reference: np.ndarray,
    observed: np.ndarray,
    sample_rate: int,
    *,
    min_delay_ms: int | None = None,
    max_delay_ms: int | None = None,
    min_correlation: float | None = None,
) -> dict:
    """Estimate a delayed echo path; never promotes the path to a physical root cause."""
    cfg = get_default_analyzer_profile().section("echo")
    min_delay_ms = int(min_delay_ms if min_delay_ms is not None else cfg["min_delay_ms"])
    max_delay_ms = int(max_delay_ms if max_delay_ms is not None else cfg["max_delay_ms"])
    min_correlation = float(min_correlation if min_correlation is not None else cfg["min_correlation"])
    high_correlation = float(cfg["high_correlation"])
    target = min(int(cfg["downsample_rate"]), sample_rate)
    max_seconds = float(cfg["max_seconds"])

    min_signal_samples=max(1,int(round(sample_rate*float(cfg["min_signal_seconds"]))))
    if reference.size < min_signal_samples or observed.size < min_signal_samples:
        return {"detected": False, "status": "INSUFFICIENT_AUDIO"}
    step = max(1, int(round(sample_rate / target)))
    a = reference.astype(np.float64, copy=False)[::step]
    b = observed.astype(np.float64, copy=False)[::step]
    n = min(a.size, b.size, int(target * max_seconds))
    a = a[:n] - float(np.mean(a[:n]))
    b = b[:n] - float(np.mean(b[:n]))
    if np.linalg.norm(a) < 1e-9 or np.linalg.norm(b) < 1e-9:
        return {"detected": False, "status": "LOW_ENERGY"}
    lo = max(1, round(min_delay_ms * target / 1000.0))
    hi = min(n // 2, round(max_delay_ms * target / 1000.0))
    best = (-1.0, None)
    for lag in range(lo, hi + 1):
        aa, bb = a[:-lag], b[lag:]
        denom = float(np.linalg.norm(aa) * np.linalg.norm(bb)) + 1e-12
        corr = float(np.dot(aa, bb) / denom)
        if abs(corr) > best[0]:
            best = (abs(corr), lag)
    corr, lag = best
    if lag is None:
        return {"detected": False, "status": "NO_PEAK"}
    delay_ms = lag * 1000.0 / target
    aa, bb = a[:-lag], b[lag:]
    rr = float(np.sqrt(np.mean(aa * aa))) + 1e-12
    orms = float(np.sqrt(np.mean(bb * bb))) + 1e-12
    level_delta = 20.0 * math.log10(orms / rr)
    quality = "HIGH" if corr >= high_correlation else "MEDIUM" if corr >= min_correlation else "LOW"
    return {
        "detected": bool(corr >= min_correlation),
        "status": "OK",
        "delay_ms": round(delay_ms, 3),
        "absolute_correlation": round(float(corr), 6),
        "quality": quality,
        "observed_to_reference_level_db": round(level_delta, 3),
        "evidence_level": "L2" if corr >= high_correlation else "L3",
        "interpretation": (
            "检测到参考音频的延迟副本，支持存在回声路径；不单独确认AEC、SLIC、声学耦合或线路混合电路中的具体根因。"
            if corr >= min_correlation
            else "未检测到达到当前门限的稳定延迟回声路径。"
        ),
    }
