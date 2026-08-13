from __future__ import annotations

import numpy as np

from app.analyzers.profile import get_default_analyzer_profile


def correlate_tracks(
    a_samples: np.ndarray,
    a_rate: int,
    a_start: float,
    b_samples: np.ndarray,
    b_rate: int,
    b_start: float,
    target_rate: int | None = None,
    max_lag_ms: int | None = None,
    max_seconds: float | None = None,
) -> dict | None:
    cfg = get_default_analyzer_profile().section("correlation")
    target_rate = int(target_rate if target_rate is not None else cfg["target_rate"])
    max_lag_ms = int(max_lag_ms if max_lag_ms is not None else cfg["max_lag_ms"])
    max_seconds = float(max_seconds if max_seconds is not None else cfg["max_seconds"])
    if a_samples.size < a_rate or b_samples.size < b_rate:
        return None
    a = _downsample(a_samples, a_rate, target_rate)
    b = _downsample(b_samples, b_rate, target_rate)
    base = min(a_start, b_start)
    a_off = max(0, round((a_start - base) * target_rate))
    b_off = max(0, round((b_start - base) * target_rate))
    total = max(a_off + a.size, b_off + b.size)
    max_total = int((max_seconds + abs(a_start - b_start) + 2.0) * target_rate)
    if total > max_total:
        total = max_total
    xa = np.zeros(total, dtype=np.float64)
    xb = np.zeros(total, dtype=np.float64)
    na = min(a.size, total - a_off)
    nb = min(b.size, total - b_off)
    if na <= target_rate or nb <= target_rate:
        return None
    xa[a_off:a_off + na] = a[:na]
    xb[b_off:b_off + nb] = b[:nb]
    xa -= np.mean(xa)
    xb -= np.mean(xb)
    max_lag = int(max_lag_ms * target_rate / 1000)
    corr = _fft_correlate(xa, xb)
    lags = np.arange(-(len(xb) - 1), len(xa))
    mask = (lags >= -max_lag) & (lags <= max_lag)
    if not np.any(mask):
        return None
    sub = corr[mask]
    sublags = lags[mask]
    idx = int(np.argmax(np.abs(sub)))
    lag = int(sublags[idx])
    score = _normalized_at_lag(xa, xb, lag)
    high = float(cfg["high_quality"])
    medium = float(cfg["medium_quality"])
    return {
        "correlation": round(float(score), 6),
        "absolute_correlation": round(float(abs(score)), 6),
        "lag_ms": round(lag * 1000.0 / target_rate, 3),
        "target_rate": target_rate,
        "quality": "HIGH" if abs(score) >= high else "MEDIUM" if abs(score) >= medium else "LOW",
        "analyzer_profile": get_default_analyzer_profile().metadata(),
    }


def _downsample(samples: np.ndarray, sample_rate: int, target_rate: int) -> np.ndarray:
    x = samples.astype(np.float64, copy=False)
    if sample_rate == target_rate:
        return x.copy()
    if sample_rate % target_rate == 0:
        factor = sample_rate // target_rate
        n = (x.size // factor) * factor
        return x[:n].reshape(-1, factor).mean(axis=1)
    duration = x.size / sample_rate
    n = max(1, int(duration * target_rate))
    old_t = np.arange(x.size) / sample_rate
    new_t = np.arange(n) / target_rate
    return np.interp(new_t, old_t, x)


def _fft_correlate(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    size = a.size + b.size - 1
    nfft = 1 << (size - 1).bit_length()
    fa = np.fft.rfft(a, nfft)
    fb = np.fft.rfft(b, nfft)
    circular = np.fft.irfft(fa * np.conj(fb), nfft)
    return np.concatenate((circular[-(b.size - 1):], circular[:a.size]))[:size]


def _normalized_at_lag(a: np.ndarray, b: np.ndarray, lag: int) -> float:
    if lag >= 0:
        aa = a[lag:]
        bb = b[:len(aa)]
    else:
        bb = b[-lag:]
        aa = a[:len(bb)]
    n = min(len(aa), len(bb))
    aa = aa[:n]
    bb = bb[:n]
    if n < 10:
        return 0.0
    denom = float(np.linalg.norm(aa) * np.linalg.norm(bb)) + 1e-12
    return float(np.dot(aa, bb) / denom)
