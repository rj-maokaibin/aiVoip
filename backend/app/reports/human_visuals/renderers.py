from __future__ import annotations

import io
import math
import wave
from typing import Iterable

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from .theme import COLORS


def _save_png(fig, *, dpi: int = 160) -> bytes:
    out = io.BytesIO()
    fig.savefig(out, format="png", dpi=dpi, bbox_inches="tight", facecolor=COLORS["background"])
    plt.close(fig)
    return out.getvalue()


def _finish_axis(ax, *, xlabel: str, ylabel: str) -> None:
    ax.set_xlabel(xlabel, fontsize=10)
    ax.set_ylabel(ylabel, fontsize=10)
    ax.grid(True, which="major", linewidth=0.6, alpha=0.35)
    ax.tick_params(labelsize=9)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)


def render_human_waveform_png(
    waveform: dict,
    *,
    anomaly_start: float | None = None,
    anomaly_end: float | None = None,
    title: str = "PCM Waveform",
    subtitle: str | None = None,
    width_px: int = 1800,
    height_px: int = 700,
) -> bytes:
    bins = list(waveform.get("bins") or [])
    width_in = width_px / 160.0
    height_in = height_px / 160.0
    fig, ax = plt.subplots(figsize=(width_in, height_in), constrained_layout=True)
    fig.patch.set_facecolor(COLORS["background"])
    ax.set_facecolor(COLORS["panel"])

    if bins:
        times = np.asarray([float(x.get("t") or 0.0) for x in bins], dtype=float)
        mins = np.asarray([float(x.get("min") or 0.0) for x in bins], dtype=float) / 32768.0
        maxs = np.asarray([float(x.get("max") or 0.0) for x in bins], dtype=float) / 32768.0
        ax.fill_between(times, mins, maxs, color=COLORS["waveform_fill"], alpha=0.72, linewidth=0)
        ax.plot(times, maxs, color=COLORS["waveform"], linewidth=0.55, alpha=0.95)
        ax.plot(times, mins, color=COLORS["waveform"], linewidth=0.55, alpha=0.95)
        duration = float(waveform.get("duration_seconds") or times[-1] or 0.0)
        ax.set_xlim(0.0, max(duration, 1e-6))
    else:
        duration = float(waveform.get("duration_seconds") or 1.0)
        ax.text(0.5, 0.5, "No waveform data", transform=ax.transAxes, ha="center", va="center", color=COLORS["muted"])
        ax.set_xlim(0.0, max(duration, 1e-6))

    ax.axhline(0.0, color=COLORS["grid"], linewidth=0.8)
    ax.set_ylim(-1.05, 1.05)

    if anomaly_start is not None:
        start = max(0.0, float(anomaly_start))
        end = float(anomaly_end if anomaly_end is not None else anomaly_start)
        end = max(start, end)
        if end <= start:
            end = start + max(0.01, duration * 0.002)
        ax.axvspan(start, min(end, duration), color=COLORS["anomaly"], alpha=0.12)
        ax.axvline(start, color=COLORS["anomaly"], linewidth=1.3, alpha=0.9)
        ax.axvline(min(end, duration), color=COLORS["anomaly"], linewidth=1.3, alpha=0.9)
        ax.text(start, 0.98, " anomaly window", color=COLORS["anomaly"], fontsize=9, va="top")

    ax.set_title(title, loc="left", fontsize=16, fontweight="semibold", color=COLORS["text"], pad=12)
    if subtitle:
        ax.text(0.0, 1.015, subtitle, transform=ax.transAxes, fontsize=9.5, color=COLORS["muted"], va="bottom")
    _finish_axis(ax, xlabel="Time (s)", ylabel="Normalized PCM amplitude")
    return _save_png(fig)


def render_human_spectrogram_png(
    spectrogram: dict,
    *,
    anomaly_start: float | None = None,
    anomaly_end: float | None = None,
    title: str = "PCM Spectrogram",
    subtitle: str | None = None,
    max_frequency_hz: float | None = None,
    dynamic_range_db: float = 80.0,
    width_px: int = 1800,
    height_px: int = 760,
) -> bytes:
    times = np.asarray(spectrogram.get("times") or [], dtype=float)
    freqs = np.asarray(spectrogram.get("frequencies") or [], dtype=float)
    raw = np.asarray(spectrogram.get("db") or [], dtype=float)

    fig, ax = plt.subplots(figsize=(width_px / 160.0, height_px / 160.0), constrained_layout=True)
    fig.patch.set_facecolor(COLORS["background"])
    ax.set_facecolor(COLORS["panel"])

    image = None
    if raw.ndim == 2 and raw.size and times.size and freqs.size:
        # Analyzer spectrogram stores FFT magnitude in dB but is not calibrated to
        # absolute dBFS. Normalize to its own maximum and label it relative dB.
        relative = raw - float(np.nanmax(raw))
        max_freq = float(max_frequency_hz or min(4000.0, float(freqs[-1])))
        mask = freqs <= max_freq
        freq_view = freqs[mask]
        data = relative[:, mask].T
        if freq_view.size:
            extent = [float(times[0]), float(times[-1]), float(freq_view[0]), float(freq_view[-1])]
            image = ax.imshow(
                data,
                origin="lower",
                aspect="auto",
                extent=extent,
                cmap="magma",
                vmin=-abs(float(dynamic_range_db)),
                vmax=0.0,
                interpolation="nearest",
            )
            ax.set_ylim(0.0, max_freq)
            ax.set_xlim(float(times[0]), max(float(times[-1]), float(times[0]) + 1e-6))
    if image is None:
        ax.text(0.5, 0.5, "No spectrogram data", transform=ax.transAxes, ha="center", va="center", color=COLORS["muted"])

    if anomaly_start is not None:
        start = max(0.0, float(anomaly_start))
        end = max(start, float(anomaly_end if anomaly_end is not None else anomaly_start))
        if end <= start:
            end = start + 0.02
        ax.axvspan(start, end, color=COLORS["anomaly"], alpha=0.12)
        ax.axvline(start, color=COLORS["anomaly"], linewidth=1.2)
        ax.axvline(end, color=COLORS["anomaly"], linewidth=1.2)

    ax.set_title(title, loc="left", fontsize=16, fontweight="semibold", color=COLORS["text"], pad=12)
    if subtitle:
        ax.text(0.0, 1.015, subtitle, transform=ax.transAxes, fontsize=9.5, color=COLORS["muted"], va="bottom")
    _finish_axis(ax, xlabel="Time (s)", ylabel="Frequency (Hz)")
    if image is not None:
        cbar = fig.colorbar(image, ax=ax, pad=0.015, fraction=0.025)
        cbar.set_label("Relative level (dB)", fontsize=9)
        cbar.ax.tick_params(labelsize=8)
    return _save_png(fig)


def _read_pcm16_wav(wav_bytes: bytes) -> tuple[np.ndarray, int, int]:
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        channels = int(wf.getnchannels())
        sample_width = int(wf.getsampwidth())
        sample_rate = int(wf.getframerate())
        frames = wf.readframes(wf.getnframes())
    if sample_width != 2:
        raise ValueError(f"HUMAN_SPECTRUM_UNSUPPORTED_SAMPLE_WIDTH:{sample_width}")
    samples = np.frombuffer(frames, dtype="<i2").astype(np.float64)
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    return samples, sample_rate, channels


def _continuous_spectrum_dbfs(samples: np.ndarray, sample_rate: int, *, max_seconds: float = 30.0) -> dict:
    if samples.size == 0:
        return {"frequencies_hz": [], "magnitude_dbfs": [], "sample_rate": sample_rate, "fft_size": 0}
    max_n = max(256, int(max_seconds * sample_rate))
    x = samples
    if x.size > max_n:
        start = (x.size - max_n) // 2
        x = x[start:start + max_n]
    x = x.astype(np.float64, copy=False)
    x = x - float(np.mean(x))
    if x.size < 16:
        return {"frequencies_hz": [], "magnitude_dbfs": [], "sample_rate": sample_rate, "fft_size": 0}

    window = np.hanning(x.size)
    coherent_gain = float(np.sum(window))
    fft_size = 1 << int(math.ceil(math.log2(max(256, x.size))))
    spectrum = np.fft.rfft(x * window, n=fft_size)
    amplitude_peak = 2.0 * np.abs(spectrum) / max(coherent_gain, 1e-12)
    dbfs = 20.0 * np.log10(np.maximum(amplitude_peak / 32768.0, 1e-12))
    dbfs = np.maximum(dbfs, -120.0)
    freqs = np.fft.rfftfreq(fft_size, d=1.0 / sample_rate)
    return {
        "frequencies_hz": freqs,
        "magnitude_dbfs": dbfs,
        "sample_rate": sample_rate,
        "fft_size": fft_size,
        "source_samples": int(x.size),
        "window": "HANN",
        "amplitude_reference": "PCM16_FULL_SCALE_32768",
        "level_unit": "dBFS",
    }


def render_human_spectrum_png_from_wav(
    wav_bytes: bytes,
    *,
    canonical_spectral: dict | None = None,
    reference_frequencies_hz: Iterable[float] | None = None,
    title: str = "PCM Spectrum",
    subtitle: str | None = None,
    min_frequency_hz: float = 30.0,
    max_frequency_hz: float = 3800.0,
    max_seconds: float = 30.0,
    width_px: int = 1800,
    height_px: int = 760,
) -> tuple[bytes, dict]:
    samples, sample_rate, channels = _read_pcm16_wav(wav_bytes)
    measurement = _continuous_spectrum_dbfs(samples, sample_rate, max_seconds=max_seconds)
    freqs = np.asarray(measurement["frequencies_hz"], dtype=float)
    dbfs = np.asarray(measurement["magnitude_dbfs"], dtype=float)
    upper = min(float(max_frequency_hz), sample_rate / 2.0 - 1e-6)
    valid = (freqs >= float(min_frequency_hz)) & (freqs <= upper)

    fig, ax = plt.subplots(figsize=(width_px / 160.0, height_px / 160.0), constrained_layout=True)
    fig.patch.set_facecolor(COLORS["background"])
    ax.set_facecolor(COLORS["panel"])

    if np.any(valid):
        f = freqs[valid]
        y = dbfs[valid]
        ax.plot(f, y, color=COLORS["spectrum"], linewidth=1.0)
        ax.fill_between(f, -120.0, y, color=COLORS["spectrum_fill"], alpha=0.30, linewidth=0)
        ax.set_xlim(float(min_frequency_hz), upper)
        ax.set_ylim(max(-120.0, float(np.nanpercentile(y, 2)) - 6.0), min(0.0, float(np.nanmax(y)) + 6.0))
        ax.set_xscale("log")
        ticks = [x for x in (30, 50, 100, 200, 500, 1000, 2000, 4000) if min_frequency_hz <= x <= upper]
        if ticks:
            ax.set_xticks(ticks)
            ax.get_xaxis().set_major_formatter(matplotlib.ticker.ScalarFormatter())
    else:
        ax.text(0.5, 0.5, "No spectrum data", transform=ax.transAxes, ha="center", va="center", color=COLORS["muted"])

    refs = [float(x) for x in (reference_frequencies_hz or []) if min_frequency_hz <= float(x) <= upper]
    for ref in refs:
        ax.axvline(ref, color=COLORS["reference"], linewidth=0.8, alpha=0.45)

    peaks = list((canonical_spectral or {}).get("peaks") or [])
    for peak in peaks[:10]:
        try:
            pf = float(peak.get("frequency_hz"))
        except (TypeError, ValueError):
            continue
        if not (min_frequency_hz <= pf <= upper) or not np.any(valid):
            continue
        index = int(np.argmin(np.abs(freqs - pf)))
        py = float(dbfs[index])
        ax.scatter([pf], [py], s=28, color=COLORS["spectrum"], zorder=4)
        ax.annotate(
            f"{pf:.1f} Hz",
            (pf, py),
            xytext=(5, 8),
            textcoords="offset points",
            fontsize=8.5,
            color=COLORS["text"],
        )

    ax.set_title(title, loc="left", fontsize=16, fontweight="semibold", color=COLORS["text"], pad=12)
    if subtitle:
        ax.text(0.0, 1.015, subtitle, transform=ax.transAxes, fontsize=9.5, color=COLORS["muted"], va="bottom")
    _finish_axis(ax, xlabel="Frequency (Hz, log scale)", ylabel="Spectrum level (dBFS)")

    png = _save_png(fig)
    metadata = {
        "measurement_method": "NUMPY_RFFT_HANN_COHERENT_GAIN_V1",
        "sample_rate": sample_rate,
        "channels": channels,
        "fft_size": int(measurement.get("fft_size") or 0),
        "source_samples": int(measurement.get("source_samples") or 0),
        "window": measurement.get("window"),
        "level_unit": "dBFS",
        "amplitude_reference": measurement.get("amplitude_reference"),
        "frequency_range_hz": [float(min_frequency_hz), float(upper)],
        "max_seconds": float(max_seconds),
    }
    return png, metadata
