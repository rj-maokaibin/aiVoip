from __future__ import annotations

import io
import wave

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from .theme import COLORS


def _ascii_label(value: str | None, fallback: str) -> str:
    raw = str(value or "")
    clean = "".join(ch if 32 <= ord(ch) <= 126 else " " for ch in raw)
    clean = " ".join(clean.split()).strip(" -|.")
    return clean or fallback


def _read_pcm16_wav(wav_bytes: bytes) -> tuple[np.ndarray, int, int]:
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        channels = int(wf.getnchannels())
        sample_width = int(wf.getsampwidth())
        sample_rate = int(wf.getframerate())
        frames = wf.readframes(wf.getnframes())
    if sample_width != 2:
        raise ValueError(f"HUMAN_SPECTROGRAM_UNSUPPORTED_SAMPLE_WIDTH:{sample_width}")
    samples = np.frombuffer(frames, dtype="<i2").astype(np.float64)
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    return samples, sample_rate, channels


def render_human_spectrogram_png_from_wav(
    wav_bytes: bytes,
    *,
    start_seconds: float | None = None,
    end_seconds: float | None = None,
    max_frequency_hz: float = 4000.0,
    n_fft: int = 512,
    hop: int = 64,
    dynamic_range_db: float = 80.0,
    reference_frequencies_hz: list[float] | tuple[float, ...] | None = None,
    title: str = "PCM High Resolution Spectrogram",
    subtitle: str | None = None,
    width_px: int = 1800,
    height_px: int = 760,
) -> tuple[bytes, dict]:
    """Render a Human-view STFT directly from source PCM16 WAV.

    This is a presentation Measurement only. It does not create or modify a
    Finding and is intentionally independent from the lower-resolution Analyzer
    spectrogram JSON used by deterministic analysis.
    """
    samples, sample_rate, channels = _read_pcm16_wav(wav_bytes)
    duration = samples.size / max(1, sample_rate)
    lo = max(0.0, float(start_seconds or 0.0))
    hi = min(duration, float(end_seconds if end_seconds is not None else duration))
    if hi <= lo:
        lo, hi = 0.0, duration
    i0 = max(0, int(round(lo * sample_rate)))
    i1 = min(samples.size, int(round(hi * sample_rate)))
    x = samples[i0:i1] / 32768.0

    n_fft = int(max(64, min(n_fft, max(64, x.size))))
    # Keep an even FFT length and a useful hop relationship.
    if n_fft % 2:
        n_fft -= 1
    hop = int(max(1, min(hop, max(1, n_fft - 1))))
    window = np.hanning(n_fft)
    rows=[]; times=[]
    if x.size >= n_fft:
        for start in range(0, x.size - n_fft + 1, hop):
            chunk=x[start:start+n_fft]
            spectrum=np.fft.rfft(chunk*window)
            magnitude=np.abs(spectrum)
            rows.append(20.0*np.log10(np.maximum(magnitude,1e-12)))
            times.append(lo + (start+n_fft/2)/sample_rate)
    elif x.size:
        padded=np.zeros(n_fft,dtype=float);padded[:x.size]=x
        spectrum=np.fft.rfft(padded*window)
        rows.append(20.0*np.log10(np.maximum(np.abs(spectrum),1e-12)))
        times.append(lo + (x.size/2)/sample_rate)

    fig,ax=plt.subplots(figsize=(width_px/160.0,height_px/160.0),constrained_layout=True)
    fig.patch.set_facecolor(COLORS["background"]);ax.set_facecolor(COLORS["panel"])
    image=None
    frequencies=np.fft.rfftfreq(n_fft,d=1.0/sample_rate)
    upper=min(float(max_frequency_hz),sample_rate/2.0)
    mask=frequencies<=upper
    if rows and np.any(mask):
        values=np.asarray(rows,dtype=float).T
        relative=values-float(np.nanmax(values))
        visible=relative[mask,:]
        time_values=np.asarray(times,dtype=float)
        left=float(time_values[0]-(hop/(2*sample_rate))) if len(time_values)>1 else lo
        right=float(time_values[-1]+(hop/(2*sample_rate))) if len(time_values)>1 else hi
        image=ax.imshow(
            visible,origin="lower",aspect="auto",
            extent=[left,right,float(frequencies[mask][0]),float(frequencies[mask][-1])],
            cmap="magma",vmin=-abs(float(dynamic_range_db)),vmax=0.0,interpolation="bilinear",
        )
        ax.set_xlim(lo,max(hi,lo+1e-6));ax.set_ylim(0.0,upper)
        for ref in reference_frequencies_hz or ():
            value=float(ref)
            if 0.0<=value<=upper:
                ax.axhline(value,color=COLORS["reference"],linewidth=0.55,alpha=0.25)
    else:
        ax.text(0.5,0.5,"No spectrogram data",transform=ax.transAxes,ha="center",va="center",color=COLORS["muted"])

    ax.set_title(_ascii_label(title,"PCM High Resolution Spectrogram"),loc="left",fontsize=16,fontweight="semibold",color=COLORS["text"],pad=28)
    subtitle_text=_ascii_label(subtitle,"") if subtitle else ""
    if subtitle_text:
        ax.text(0.0,1.015,subtitle_text,transform=ax.transAxes,fontsize=9.5,color=COLORS["muted"],va="bottom")
    ax.set_xlabel("Time (s)",fontsize=10);ax.set_ylabel("Frequency (Hz)",fontsize=10)
    ax.tick_params(labelsize=9);ax.spines["top"].set_visible(False);ax.spines["right"].set_visible(False)
    if image is not None:
        cbar=fig.colorbar(image,ax=ax,pad=0.015,fraction=0.025)
        cbar.set_label("Relative level (dB)",fontsize=9);cbar.ax.tick_params(labelsize=8)
    out=io.BytesIO();fig.savefig(out,format="png",dpi=160,bbox_inches="tight",facecolor=COLORS["background"]);plt.close(fig)
    return out.getvalue(),{
        "measurement_method":"NUMPY_STFT_HANN_RELATIVE_DB_V1",
        "sample_rate":sample_rate,
        "channels":channels,
        "n_fft":n_fft,
        "hop":hop,
        "time_window_seconds":[round(lo,6),round(hi,6)],
        "frequency_range_hz":[0.0,float(upper)],
        "level_unit":"relative dB",
        "absolute_dbfs":False,
        "dynamic_range_db":float(dynamic_range_db),
    }
