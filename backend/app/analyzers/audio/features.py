from __future__ import annotations

import math
from dataclasses import dataclass
import numpy as np

from app.analyzers.profile import get_default_analyzer_profile


def _dbfs(value: float) -> float:
    if value <= 0:
        return -120.0
    return 20.0 * math.log10(value / 32768.0)


def waveform_envelope(samples: np.ndarray, sample_rate: int, max_bins: int = 1200) -> dict:
    x = samples.astype(np.float64, copy=False)
    if x.size == 0:
        return {'sample_rate': sample_rate, 'duration_seconds': 0.0, 'bins': []}
    bin_size = max(1, int(math.ceil(x.size / max_bins)))
    bins = []
    for start in range(0, x.size, bin_size):
        chunk = x[start:start + bin_size]
        rms = float(np.sqrt(np.mean(chunk * chunk))) if chunk.size else 0.0
        bins.append({
            't': round(start / sample_rate, 6),
            'min': int(np.min(chunk)) if chunk.size else 0,
            'max': int(np.max(chunk)) if chunk.size else 0,
            'rms_dbfs': round(_dbfs(rms), 3),
        })
    return {
        'sample_rate': sample_rate,
        'duration_seconds': round(x.size / sample_rate, 6),
        'bin_size_samples': bin_size,
        'bins': bins,
    }


def spectrogram_data(samples: np.ndarray, sample_rate: int, n_fft: int = 256, hop: int = 128,
                     max_time_bins: int = 256, max_freq_bins: int = 128) -> dict:
    x = samples.astype(np.float64, copy=False)
    if x.size < n_fft:
        return {'sample_rate': sample_rate, 'n_fft': n_fft, 'hop': hop, 'times': [], 'frequencies': [], 'db': []}
    window = np.hanning(n_fft)
    frames = []
    starts = list(range(0, x.size - n_fft + 1, hop))
    time_stride = max(1, math.ceil(len(starts) / max_time_bins))
    starts = starts[::time_stride]
    freq_values = np.fft.rfftfreq(n_fft, d=1.0 / sample_rate)
    freq_stride = max(1, math.ceil(freq_values.size / max_freq_bins))
    selected_freqs = freq_values[::freq_stride]
    for start in starts:
        mag = np.abs(np.fft.rfft(x[start:start + n_fft] * window))
        db = 20.0 * np.log10(np.maximum(mag, 1e-9))
        frames.append(np.round(db[::freq_stride], 2).tolist())
    return {
        'sample_rate': sample_rate,
        'n_fft': n_fft,
        'hop': hop,
        'time_stride': time_stride,
        'freq_stride': freq_stride,
        'times': [round(s / sample_rate, 6) for s in starts],
        'frequencies': np.round(selected_freqs, 3).tolist(),
        'db': frames,
    }


def detect_silence(samples: np.ndarray, sample_rate: int, frame_ms: int | None = None, min_duration_ms: int | None = None) -> list[dict]:
    """Legacy raw-silence utility kept for diagnostics; thresholds come from AnalyzerProfile."""
    cfg=get_default_analyzer_profile().section("silence")
    frame_ms=int(frame_ms if frame_ms is not None else cfg["frame_ms"])
    # Legacy detector historically used 80 ms. Keep explicit API compatibility, but when no
    # override is supplied use the canonical unexpected-silence duration from the profile.
    min_duration_ms=int(min_duration_ms if min_duration_ms is not None else cfg["min_duration_ms"])
    x=samples.astype(np.float64,copy=False)
    frame=max(1,int(sample_rate*frame_ms/1000))
    if x.size<frame:
        return []
    rms=[]
    for start in range(0,x.size-frame+1,frame):
        chunk=x[start:start+frame]
        rms.append(float(np.sqrt(np.mean(chunk*chunk))))
    levels=np.array([_dbfs(v) for v in rms],dtype=np.float64)
    floor=float(np.percentile(levels,float(cfg["noise_floor_percentile"]))) if levels.size else -60.0
    threshold=min(float(cfg["threshold_max_dbfs"]),max(float(cfg["threshold_min_dbfs"]),floor+float(cfg["noise_floor_margin_db"])))
    quiet=levels<=threshold
    min_frames=max(1,math.ceil(min_duration_ms/frame_ms))
    out=[]; start_i=None
    for i,is_quiet in enumerate(np.r_[quiet,False]):
        if is_quiet and start_i is None:
            start_i=i
        elif not is_quiet and start_i is not None:
            count=i-start_i
            if count>=min_frames:
                out.append({
                    "type":"SILENCE",
                    "start_seconds":round(start_i*frame_ms/1000.0,6),
                    "end_seconds":round(i*frame_ms/1000.0,6),
                    "duration_ms":round(count*frame_ms,3),
                    "threshold_dbfs":round(threshold,3),
                    "median_dbfs":round(float(np.median(levels[start_i:i])),3),
                })
            start_i=None
    return out


def detect_click_pop(samples: np.ndarray, sample_rate: int, min_jump: float | None = None) -> list[dict]:
    """Legacy single-feature detector retained for compatibility, profile-backed and non-confirmatory."""
    cfg=get_default_analyzer_profile().section("click_pop")
    min_jump=float(min_jump if min_jump is not None else cfg["min_jump"])
    x=samples.astype(np.float64,copy=False)
    if x.size<3:
        return []
    diff=np.abs(np.diff(x)); median=float(np.median(diff)); mad=float(np.median(np.abs(diff-median)))+1e-9
    threshold=max(min_jump,median+float(cfg["mad_multiplier"])*mad)
    candidates=np.flatnonzero(diff>=threshold)
    out=[]; last=-10**9
    merge_samples=max(1,int(sample_rate*float(cfg["merge_ms"])/1000.0))
    short_ms=float(cfg["short_window_ms"])
    for idx in candidates:
        if idx-last<=merge_samples:
            if out and diff[idx]>out[-1]["jump"]:
                out[-1]["jump"]=round(float(diff[idx]),3); out[-1]["time_seconds"]=round((idx+1)/sample_rate,6)
            last=idx; continue
        lo=max(0,idx-int(sample_rate*short_ms/1000.0)); hi=min(x.size,idx+int(sample_rate*short_ms/1000.0))
        local=x[lo:hi]; local_rms=float(np.sqrt(np.mean(local*local))) if local.size else 0.0
        out.append({
            "type":"CLICK_POP",
            "time_seconds":round((idx+1)/sample_rate,6),
            "jump":round(float(diff[idx]),3),
            "threshold":round(threshold,3),
            "local_rms_dbfs":round(_dbfs(local_rms),3),
            "evidence_level":"L3",
        })
        last=idx
    return out[:int(cfg["max_events"])]


def spectral_tone_analysis(samples: np.ndarray, sample_rate: int, max_seconds: float | None = None) -> dict:
    cfg=get_default_analyzer_profile().section("spectral")
    max_seconds=float(max_seconds if max_seconds is not None else cfg["max_seconds"])
    x=samples.astype(np.float64,copy=False)
    if x.size==0:
        return {"peaks":[],"narrowband_tones":[],"comb":None}
    max_n=int(max_seconds*sample_rate)
    if x.size>max_n:
        start=(x.size-max_n)//2; x=x[start:start+max_n]
    x=x-float(np.mean(x))
    n=1<<int(math.ceil(math.log2(max(256,x.size))))
    mag=np.abs(np.fft.rfft(x*np.hanning(x.size),n=n)); power=mag*mag
    freqs=np.fft.rfftfreq(n,1.0/sample_rate)
    max_freq=min(float(cfg["max_frequency_hz"]),sample_rate/2.0-1.0)
    valid=(freqs>=float(cfg["min_frequency_hz"]))&(freqs<=max_freq)
    freq_v=freqs[valid]; power_v=power[valid]; total=float(np.sum(power_v))+1e-12
    if power_v.size==0:
        return {"peaks":[],"narrowband_tones":[],"comb":None}
    candidate_count=min(int(cfg["candidate_peaks"]),power_v.size)
    idxs=np.argpartition(power_v,-candidate_count)[-candidate_count:]; idxs=idxs[np.argsort(power_v[idxs])[::-1]]
    peaks=[]
    for i in idxs:
        f=float(freq_v[i]); ratio=float(power_v[i]/total)
        if any(abs(f-p["frequency_hz"])<float(cfg["peak_merge_hz"]) for p in peaks):
            continue
        peaks.append({"frequency_hz":round(f,3),"energy_ratio":round(ratio,8)})
        if len(peaks)>=int(cfg["max_peaks"]): break
    narrow=[p for p in peaks if p["energy_ratio"]>=float(cfg["narrowband_energy_ratio"])]
    comb=_detect_comb(peaks,cfg)
    return {"peaks":peaks,"narrowband_tones":narrow,"comb":comb}


def _detect_comb(peaks: list[dict], cfg: dict | None = None) -> dict | None:
    cfg=cfg or get_default_analyzer_profile().section("spectral")
    fs=sorted(p["frequency_hz"] for p in peaks[:10])
    if len(fs)<int(cfg["comb_min_members"]): return None
    best=None
    for spacing in range(int(cfg["comb_spacing_min_hz"]),int(cfg["comb_spacing_max_hz"])+1,int(cfg["comb_spacing_step_hz"])):
        members=[]
        for f in fs:
            harmonic=max(1,round(f/spacing)); err=abs(f-harmonic*spacing)
            if err<=max(float(cfg["comb_error_min_hz"]),spacing*float(cfg["comb_error_ratio"])):
                members.append(f)
        if len(members)>=int(cfg["comb_min_members"]) and (best is None or len(members)>len(best["members"])):
            best={"spacing_hz":float(spacing),"members":members}
    if not best: return None
    return {
        "detected":True,
        "spacing_hz":round(best["spacing_hz"],3),
        "member_count":len(best["members"]),
        "frequencies_hz":[round(x,3) for x in best["members"]],
    }

