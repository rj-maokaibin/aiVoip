from __future__ import annotations

import math

import numpy as np

from app.analyzers.profile import get_default_analyzer_profile


def _dbfs_rms(samples: np.ndarray) -> float:
    x = samples.astype(np.float64, copy=False)
    if x.size == 0:
        return -120.0
    rms = float(np.sqrt(np.mean(x * x)))
    if rms <= 0:
        return -120.0
    return 20.0 * math.log10(rms / 32768.0)


def normalized_autocorrelation(samples: np.ndarray, lag_samples: int) -> float:
    x = samples.astype(np.float64, copy=False)
    if lag_samples <= 0 or x.size <= lag_samples + 8:
        return 0.0
    a = x[:-lag_samples] - float(np.mean(x[:-lag_samples]))
    b = x[lag_samples:] - float(np.mean(x[lag_samples:]))
    denom = float(np.linalg.norm(a) * np.linalg.norm(b)) + 1e-12
    return float(np.dot(a, b) / denom)


def autocorrelation_ms(samples: np.ndarray, sample_rate: int, lag_ms: float) -> float:
    lag = max(1, round(sample_rate * lag_ms / 1000.0))
    return normalized_autocorrelation(samples, lag)


def _periodicity_score(ac10: float, ac20: float, ac40: float) -> float:
    score = (max(0.0, -ac10) + max(0.0, ac20) + max(0.0, ac40)) / 3.0
    return float(max(0.0, min(1.0, score)))


def _estimate_period_ms(samples: np.ndarray, sample_rate: int, cfg: dict) -> dict:
    lo_ms = float(cfg["period_search_min_ms"])
    hi_ms = float(cfg["period_search_max_ms"])
    step_ms = float(cfg["period_search_step_ms"])
    best = None
    step_samples = max(1, round(sample_rate * step_ms / 1000.0))
    lo = max(1, round(sample_rate * lo_ms / 1000.0))
    hi = max(lo + 1, round(sample_rate * hi_ms / 1000.0))
    for lag in range(lo, hi + 1, step_samples):
        corr = normalized_autocorrelation(samples, lag)
        if best is None or corr > best[1]:
            best = (lag, corr)
    if best is None:
        return {"period_ms": None, "frequency_hz": None, "correlation": 0.0}
    lag, corr = best
    period_ms = lag * 1000.0 / sample_rate
    return {
        "period_ms": round(period_ms, 3),
        "frequency_hz": round(1000.0 / period_ms, 3) if period_ms else None,
        "correlation": round(float(corr), 6),
    }


def odd_50hz_harmonic_comb(
    samples: np.ndarray,
    sample_rate: int,
    start_hz: int | None = None,
    end_hz: int | None = None,
    step_hz: int | None = None,
    prominence_db: float | None = None,
) -> dict:
    """Detect the validated odd-50-Hz harmonic pattern without requiring a 50-Hz fundamental."""
    cfg = dict(get_default_analyzer_profile().section("periodic"))
    start_hz = int(start_hz if start_hz is not None else cfg["comb_start_hz"])
    end_hz = int(end_hz if end_hz is not None else cfg["comb_end_hz"])
    step_hz = int(step_hz if step_hz is not None else cfg["comb_step_hz"])
    prominence_db = float(prominence_db if prominence_db is not None else cfg["comb_prominence_db"])
    target_tol = float(cfg["comb_target_tolerance_hz"])
    exclusion = float(cfg["comb_exclusion_hz"])
    background = float(cfg["comb_background_hz"])
    detect_min_hits = int(cfg["comb_detect_min_hits"])

    x = samples.astype(np.float64, copy=False)
    if x.size < max(256, sample_rate // 2):
        return {
            "detected": False,
            "base_hz": 50.0,
            "spacing_hz": 100.0,
            "hit_count": 0,
            "target_count": 0,
            "members": [],
            "mean_prominence_db": 0.0,
        }
    x = x - float(np.mean(x))
    power = np.abs(np.fft.rfft(x * np.hanning(x.size))) ** 2
    freqs = np.fft.rfftfreq(x.size, 1.0 / sample_rate)
    members = []
    for target in range(start_hz, end_hz + 1, step_hz):
        target_mask = (freqs >= target - target_tol) & (freqs <= target + target_tol)
        bg_mask = (
            (freqs >= target - background)
            & (freqs <= target + background)
            & ~((freqs >= target - exclusion) & (freqs <= target + exclusion))
        )
        if not np.any(target_mask) or not np.any(bg_mask):
            continue
        target_indices = np.flatnonzero(target_mask)
        peak_idx = int(target_indices[int(np.argmax(power[target_mask]))])
        peak_power = float(power[peak_idx])
        bg = float(np.median(power[bg_mask])) + 1e-18
        prom = 10.0 * math.log10((peak_power + 1e-18) / bg)
        members.append(
            {
                "target_hz": float(target),
                "peak_hz": round(float(freqs[peak_idx]), 3),
                "prominence_db": round(prom, 3),
                "hit": bool(prom >= prominence_db),
            }
        )
    hits = [m for m in members if m["hit"]]
    mean_prom = float(np.mean([m["prominence_db"] for m in hits])) if hits else 0.0
    return {
        "detected": len(hits) >= detect_min_hits,
        "pattern": "ODD_50HZ_HARMONIC_COMB",
        "base_hz": 50.0,
        "spacing_hz": 100.0,
        "hit_count": len(hits),
        "target_count": len(members),
        "members": members,
        "mean_prominence_db": round(mean_prom, 3),
    }


def _window_metrics(samples: np.ndarray, sample_rate: int, start_sample: int, window_samples: int, cfg: dict) -> dict:
    chunk = samples[start_sample:start_sample + window_samples]
    lags = [float(x) for x in cfg["ac_lags_ms"]]
    if len(lags) != 3:
        raise ValueError("ANALYZER_PROFILE_PERIODIC_AC_LAGS_REQUIRES_3")
    ac10 = autocorrelation_ms(chunk, sample_rate, lags[0])
    ac20 = autocorrelation_ms(chunk, sample_rate, lags[1])
    ac40 = autocorrelation_ms(chunk, sample_rate, lags[2])
    score = _periodicity_score(ac10, ac20, ac40)
    return {
        "start_sample": start_sample,
        "start_seconds": round(start_sample / sample_rate, 6),
        "duration_seconds": round(chunk.size / sample_rate, 6),
        "rms_dbfs": round(_dbfs_rms(chunk), 3),
        "autocorrelation": {
            f"{lags[0]:g}ms": round(ac10, 6),
            f"{lags[1]:g}ms": round(ac20, 6),
            f"{lags[2]:g}ms": round(ac40, 6),
        },
        "periodicity_score": round(score, 6),
    }


def analyze_low_energy_periodicity(
    samples: np.ndarray,
    sample_rate: int,
    window_seconds: float | None = None,
    hop_seconds: float | None = None,
    low_energy_quantile: float | None = None,
    max_candidates: int | None = None,
) -> dict:
    cfg = dict(get_default_analyzer_profile().section("periodic"))
    window_seconds = float(window_seconds if window_seconds is not None else cfg["window_seconds"])
    hop_seconds = float(hop_seconds if hop_seconds is not None else cfg["hop_seconds"])
    low_energy_quantile = float(low_energy_quantile if low_energy_quantile is not None else cfg["low_energy_quantile"])
    max_candidates = int(max_candidates if max_candidates is not None else cfg["max_candidates"])
    min_window_seconds = float(cfg["min_window_seconds"])

    x = samples.astype(np.int16, copy=False)
    win = max(round(sample_rate * window_seconds), round(sample_rate * min_window_seconds))
    hop = max(1, round(sample_rate * hop_seconds))
    if x.size < win:
        return {"status": "INSUFFICIENT_AUDIO", "candidate_count": 0, "representative": None, "level": "LOW"}
    raw = [_window_metrics(x, sample_rate, start, win, cfg) for start in range(0, x.size - win + 1, hop)]
    if not raw:
        return {"status": "INSUFFICIENT_AUDIO", "candidate_count": 0, "representative": None, "level": "LOW"}
    levels = np.array([r["rms_dbfs"] for r in raw], dtype=np.float64)
    q = max(float(cfg["low_energy_quantile_min"]), min(float(cfg["low_energy_quantile_max"]), low_energy_quantile))
    threshold = float(np.quantile(levels, q))
    low = [r for r in raw if r["rms_dbfs"] <= threshold + 1e-9]
    low.sort(key=lambda r: (r["periodicity_score"], -r["rms_dbfs"]), reverse=True)
    candidates = low[:max_candidates]
    representative = candidates[0] if candidates else min(raw, key=lambda r: r["rms_dbfs"])
    start = int(representative["start_sample"])
    chunk = x[start:start + win]
    comb = odd_50hz_harmonic_comb(chunk, sample_rate)
    period = _estimate_period_ms(chunk, sample_rate, cfg)
    ac = representative["autocorrelation"]
    lags = [float(x) for x in cfg["ac_lags_ms"]]
    a10, a20, a40 = (ac[f"{lag:g}ms"] for lag in lags)
    strong_cfg = cfg["strong"]
    medium_cfg = cfg["medium"]
    strong_periodic = (
        a20 >= float(strong_cfg["ac20_min"])
        and a40 >= float(strong_cfg["ac40_min"])
        and a10 <= float(strong_cfg["ac10_max"])
    )
    medium_periodic = (
        a20 >= float(medium_cfg["ac20_min"])
        and a40 >= float(medium_cfg["ac40_min"])
        and a10 <= float(medium_cfg["ac10_max"])
    )
    hits = int(comb.get("hit_count", 0))
    if strong_periodic and hits >= int(strong_cfg["min_comb_hits"]):
        level = "HIGH"
    elif strong_periodic or (medium_periodic and hits >= int(medium_cfg["min_comb_hits"])):
        level = "MEDIUM"
    else:
        level = "LOW"
    representative = dict(representative)
    representative.pop("start_sample", None)
    return {
        "status": "OK",
        "method": "LOW_ENERGY_WINDOW_ACF_AND_ODD_HARMONIC_COMB",
        "window_seconds": window_seconds,
        "hop_seconds": hop_seconds,
        "low_energy_threshold_dbfs": round(threshold, 3),
        "candidate_count": len(low),
        "representative": representative,
        "estimated_period": period,
        "comb": comb,
        "level": level,
        "interpretation": (
            "检测到约20ms重复周期并伴随150/250/350/...Hz奇次谐波梳状结构；该模式与50Hz工频相关周期干扰具有较强一致性，但不能单独确认具体电源/接地/话机/SLIC根因。"
            if level == "HIGH"
            else "存在部分周期/梳状谱特征，但当前强度不足以作为稳定周期干扰的直接证据。"
            if level == "MEDIUM"
            else "未检测到满足当前门限的稳定20ms周期+奇次谐波梳状结构。"
        ),
    }


def slice_by_absolute_time(samples: np.ndarray, sample_rate: int, start_time: float, window_start: float, window_end: float) -> np.ndarray:
    a = max(0, int(round((window_start - start_time) * sample_rate)))
    b = min(samples.size, int(round((window_end - start_time) * sample_rate)))
    if b <= a:
        return np.zeros(0, dtype=samples.dtype)
    return samples[a:b]


def periodic_strength(result: dict | None) -> float:
    if not result or result.get("status") != "OK" or not result.get("representative"):
        return 0.0
    cfg = get_default_analyzer_profile().section("periodic")["strength"]
    rep = result["representative"]
    score = float(rep.get("periodicity_score", 0.0))
    hits = float((result.get("comb") or {}).get("hit_count", 0))
    comb_score = min(1.0, hits / float(cfg["comb_full_hits"]))
    return max(
        0.0,
        min(1.0, float(cfg["periodicity_weight"]) * score + float(cfg["comb_weight"]) * comb_score),
    )
