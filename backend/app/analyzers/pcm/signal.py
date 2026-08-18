from __future__ import annotations

import math
from typing import Iterable
import numpy as np

from app.analyzers.profile import get_default_analyzer_profile

DTMF_ROWS = {697: "1", 770: "4", 852: "7", 941: "*"}
DTMF_COLS = {1209: 0, 1336: 1, 1477: 2, 1633: 3}
DTMF_GRID = {
    697: ["1", "2", "3", "A"],
    770: ["4", "5", "6", "B"],
    852: ["7", "8", "9", "C"],
    941: ["*", "0", "#", "D"],
}


def basic_stats(samples: np.ndarray) -> dict:
    if samples.size == 0:
        return {"sample_count": 0}
    x = samples.astype(np.float64, copy=False)
    rms = float(np.sqrt(np.mean(x * x)))
    peak = float(np.max(np.abs(x)))
    rms_dbfs = 20.0 * math.log10(rms / 32768.0) if rms > 0 else None
    peak_dbfs = 20.0 * math.log10(peak / 32768.0) if peak > 0 else None
    clip_threshold=float(get_default_analyzer_profile().section("basic_metrics")["clipping_abs_threshold"])
    return {
        "sample_count": int(samples.size),
        "rms": round(rms, 6),
        "dbfs": round(rms_dbfs, 6) if rms_dbfs is not None else None,
        "rms_dbfs": round(rms_dbfs, 6) if rms_dbfs is not None else None,
        "peak": round(peak, 6),
        "peak_dbfs": round(peak_dbfs, 6) if peak_dbfs is not None else None,
        "dc_offset": round(float(np.mean(x)), 6),
        "clipping_percent": round(float(np.mean(np.abs(x) >= clip_threshold) * 100.0), 8),
        "clipping_abs_threshold": clip_threshold,
        "level_unit": "dBFS",
        "level_boundary": "dBFS为相对于数字满量程的数字电平，不等价于实际声压级dB SPL。",
    }


def goertzel_power(samples: np.ndarray, sample_rate: int, frequency: float) -> float:
    if samples.size == 0:
        return 0.0
    x = samples.astype(np.float64, copy=False)
    k = int(0.5 + (len(x) * frequency / sample_rate))
    omega = 2.0 * math.pi * k / len(x)
    coeff = 2.0 * math.cos(omega)
    q0 = q1 = q2 = 0.0
    for value in x:
        q0 = coeff * q1 - q2 + value
        q2, q1 = q1, q0
    return max(0.0, q1 * q1 + q2 * q2 - coeff * q1 * q2)


def hum_analysis(samples: np.ndarray, sample_rate: int) -> dict:
    """Relative mains-family spectral evidence; never a physical root-cause assertion."""
    cfg=get_default_analyzer_profile().section("hum")
    freqs=[int(f) for f in cfg["frequencies_hz"]]
    powers={f:goertzel_power(samples,sample_rate,f) for f in freqs}
    total=float(np.sum(samples.astype(np.float64)**2))+1e-12
    families={
        "50Hz":sum(powers[f] for f in [int(x) for x in cfg["family_50_hz"]] if f in powers),
        "60Hz":sum(powers[f] for f in [int(x) for x in cfg["family_60_hz"]] if f in powers),
    }
    norm=max(1,samples.size)
    ratios={k:float(v/(total*norm)) for k,v in families.items()}
    dominant=max(ratios,key=ratios.get); score=ratios[dominant]
    level="HIGH" if score>=float(cfg["high_score"]) else "MEDIUM" if score>=float(cfg["medium_score"]) else "LOW"
    return {
        "dominant_family":dominant,
        "score":round(score,6),
        "level":level,
        "component_scores":{k:round(v,6) for k,v in ratios.items()},
        "evidence_boundary":"频谱家族证据不能单独确认电源、接地、话机或SLIC根因。",
    }

def detect_dtmf(samples: np.ndarray, sample_rate: int, frame_ms: int | None = None, hop_ms: int | None = None) -> list[dict]:
    """Detect high-confidence in-band DTMF events.

    The detector intentionally rejects broad tonal/comb noise by requiring:
    * both row and column tones to own a meaningful share of frame energy;
    * strong dominance over the second-best DTMF row/column;
    * reasonable row/column twist;
    * persistence for >=60 ms.

    Results are still evidence candidates until cross-checked with RFC2833,
    SIP INFO or call/dial context.
    """
    cfg=get_default_analyzer_profile().section("dtmf")
    frame_ms=int(frame_ms if frame_ms is not None else cfg["frame_ms"])
    hop_ms=int(hop_ms if hop_ms is not None else cfg["hop_ms"])
    frame = max(1, int(sample_rate * frame_ms / 1000))
    hop = max(1, int(sample_rate * hop_ms / 1000))
    rows = list(DTMF_GRID)
    cols = list(DTMF_COLS)
    raw: list[tuple[str, float, float, float]] = []
    for start in range(0, max(0, samples.size - frame + 1), hop):
        chunk = samples[start:start + frame].astype(np.float64, copy=False)
        sum_sq = float(np.sum(chunk * chunk))
        rms = math.sqrt(sum_sq / max(1, len(chunk)))
        if rms < float(cfg["min_rms"]):
            continue
        norm = sum_sq * max(1, len(chunk)) + 1e-9
        rp = {f: goertzel_power(chunk, sample_rate, f) for f in rows}
        cp = {f: goertzel_power(chunk, sample_rate, f) for f in cols}
        r = max(rp, key=rp.get); c = max(cp, key=cp.get)
        rv = sorted(rp.values(), reverse=True); cv = sorted(cp.values(), reverse=True)
        row_dom = rp[r] / max(rv[1], 1e-9)
        col_dom = cp[c] / max(cv[1], 1e-9)
        row_energy = rp[r] / norm
        col_energy = cp[c] / norm
        twist_db = 10.0 * math.log10((rp[r] + 1e-9) / (cp[c] + 1e-9))
        if row_dom < float(cfg["min_row_dominance"]) or col_dom < float(cfg["min_col_dominance"]):
            continue
        if row_energy < float(cfg["min_row_energy"]) or col_energy < float(cfg["min_col_energy"]):
            continue
        if abs(twist_db) > float(cfg["max_abs_twist_db"]):
            continue
        confidence = min(1.0, min(row_dom, col_dom) / float(cfg["dominance_confidence_full_scale"]), min(row_energy, col_energy) / float(cfg["energy_confidence_full_scale"]))
        digit = DTMF_GRID[r][DTMF_COLS[c]]
        raw.append((digit, start / sample_rate, (start + frame) / sample_rate, confidence))

    merged: list[dict] = []
    for digit, start, end, confidence in raw:
        if merged and merged[-1]["digit"] == digit and start <= merged[-1]["end_seconds"] + hop_ms / 1000.0 + 1e-6:
            merged[-1]["end_seconds"] = round(end, 6)
            merged[-1]["duration_ms"] = round((end - merged[-1]["start_seconds"]) * 1000.0, 3)
            merged[-1]["confidence"] = round(max(merged[-1]["confidence"], confidence), 6)
        else:
            merged.append({
                "digit": digit,
                "start_seconds": round(start, 6),
                "end_seconds": round(end, 6),
                "duration_ms": round((end-start)*1000.0, 3),
                "confidence": round(confidence, 6),
                "classification": "DTMF_CANDIDATE",
            })
    return [event for event in merged if event["duration_ms"] >= float(cfg["min_duration_ms"])]


def dtmf_sequence(events: list[dict], max_interdigit_gap_ms: float | None = None) -> list[dict]:
    """Group DTMF events into dial-like sequences without inventing missing digits."""
    if not events:
        return []
    if max_interdigit_gap_ms is None:
        max_interdigit_gap_ms=float(get_default_analyzer_profile().section("dtmf")["max_interdigit_gap_ms"])
    sequences: list[dict] = []
    for event in events:
        if not sequences or (event["start_seconds"] - sequences[-1]["end_seconds"]) * 1000.0 > max_interdigit_gap_ms:
            sequences.append({
                "digits": event["digit"],
                "start_seconds": event["start_seconds"],
                "end_seconds": event["end_seconds"],
                "event_count": 1,
                "min_confidence": event["confidence"],
            })
        else:
            seq = sequences[-1]
            seq["digits"] += event["digit"]
            seq["end_seconds"] = event["end_seconds"]
            seq["event_count"] += 1
            seq["min_confidence"] = round(min(seq["min_confidence"], event["confidence"]), 6)
    return sequences
