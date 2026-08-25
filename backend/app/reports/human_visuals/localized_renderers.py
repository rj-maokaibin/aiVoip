from __future__ import annotations

import io
import wave

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import ScalarFormatter
import numpy as np

from .renderers import (
    _auto_focus,
    _continuous_spectrum_dbfs,
    _read_pcm16_wav,
    render_human_spectrum_png_from_wav as _fallback_spectrum,
    render_human_spectrogram_png as _fallback_spectrogram,
    render_human_waveform_png as _fallback_waveform,
)
from .theme import COLORS
from .typography import (
    human_cjk_font_available,
    human_font_properties,
    human_font_status,
    localized_text,
    localized_title,
)
from .wav_spectrogram import render_human_spectrogram_png_from_wav as _fallback_wav_spectrogram


def _save_png(fig, *, dpi: int = 160) -> bytes:
    out = io.BytesIO()
    fig.savefig(out, format="png", dpi=dpi, bbox_inches="tight", facecolor=COLORS["background"])
    plt.close(fig)
    return out.getvalue()


def _axis(ax, *, xlabel_zh: str, xlabel_en: str, ylabel_zh: str, ylabel_en: str) -> None:
    fp = human_font_properties(size=10)
    ax.set_xlabel(localized_text(xlabel_zh, xlabel_en), fontproperties=fp)
    ax.set_ylabel(localized_text(ylabel_zh, ylabel_en), fontproperties=fp)
    ax.grid(True, which="major", linewidth=0.6, alpha=0.35)
    ax.tick_params(labelsize=9)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)


def _title(ax, title: str, subtitle: str | None, fallback: str) -> None:
    ax.set_title(
        localized_title(title, fallback),
        loc="left",
        fontproperties=human_font_properties(size=16, weight="semibold"),
        color=COLORS["text"],
        pad=28,
    )
    if subtitle:
        ax.text(
            0.0,
            1.015,
            subtitle,
            transform=ax.transAxes,
            fontproperties=human_font_properties(size=9.5),
            color=COLORS["muted"],
            va="bottom",
        )


def render_human_waveform_png(
    waveform: dict,
    *,
    anomaly_start: float | None = None,
    anomaly_end: float | None = None,
    display_start: float | None = None,
    display_end: float | None = None,
    auto_vertical_scale: bool | None = None,
    title: str = "PCM Waveform",
    subtitle: str | None = None,
    width_px: int = 1800,
    height_px: int = 700,
) -> bytes:
    if not human_cjk_font_available():
        return _fallback_waveform(
            waveform,
            anomaly_start=anomaly_start,
            anomaly_end=anomaly_end,
            display_start=display_start,
            display_end=display_end,
            auto_vertical_scale=auto_vertical_scale,
            title=title,
            subtitle=subtitle,
            width_px=width_px,
            height_px=height_px,
        )

    bins = list(waveform.get("bins") or [])
    fig, ax = plt.subplots(figsize=(width_px / 160.0, height_px / 160.0), constrained_layout=True)
    fig.patch.set_facecolor(COLORS["background"])
    ax.set_facecolor(COLORS["panel"])

    duration = float(waveform.get("duration_seconds") or 1.0)
    view_lo, view_hi, auto_focused = _auto_focus(duration, anomaly_start, anomaly_end, display_start, display_end)
    if auto_vertical_scale is None:
        auto_vertical_scale = bool(auto_focused)

    selected_mins: np.ndarray | None = None
    selected_maxs: np.ndarray | None = None
    if bins:
        times = np.asarray([float(x.get("t") or 0.0) for x in bins], dtype=float)
        mins = np.asarray([float(x.get("min") or 0.0) for x in bins], dtype=float) / 32768.0
        maxs = np.asarray([float(x.get("max") or 0.0) for x in bins], dtype=float) / 32768.0
        visible = (times >= view_lo) & (times <= view_hi)
        if not np.any(visible):
            visible = np.ones_like(times, dtype=bool)
        vt = times[visible]
        selected_mins = mins[visible]
        selected_maxs = maxs[visible]
        ax.fill_between(vt, selected_mins, selected_maxs, color=COLORS["waveform_fill"], alpha=0.72, linewidth=0)
        ax.plot(vt, selected_maxs, color=COLORS["waveform"], linewidth=0.55, alpha=0.95)
        ax.plot(vt, selected_mins, color=COLORS["waveform"], linewidth=0.55, alpha=0.95)
    else:
        ax.text(
            0.5, 0.5, localized_text("无波形数据", "No waveform data"),
            transform=ax.transAxes, ha="center", va="center",
            fontproperties=human_font_properties(size=10), color=COLORS["muted"],
        )

    ax.set_xlim(view_lo, max(view_hi, view_lo + 1e-6))
    ax.axhline(0.0, color=COLORS["grid"], linewidth=0.8)
    if auto_vertical_scale and selected_mins is not None and selected_maxs is not None and selected_mins.size:
        peak = float(max(np.max(np.abs(selected_mins)), np.max(np.abs(selected_maxs))))
        y_limit = min(1.05, max(0.005, peak * 1.18))
        ax.set_ylim(-y_limit, y_limit)
        ax.text(
            0.995, 0.965,
            localized_text(f"纵向自动放大：±{y_limit:.4f} FS", f"vertical zoom: +/-{y_limit:.4f} FS"),
            transform=ax.transAxes, ha="right", va="top",
            fontproperties=human_font_properties(size=8.5), color=COLORS["muted"],
        )
    else:
        ax.set_ylim(-1.05, 1.05)

    if anomaly_start is not None:
        start = max(0.0, float(anomaly_start))
        end = max(start, float(anomaly_end if anomaly_end is not None else anomaly_start))
        if end <= start:
            end = start + max(0.01, duration * 0.002)
        ax.axvspan(start, min(end, duration), color=COLORS["anomaly"], alpha=0.12)
        ax.axvline(start, color=COLORS["anomaly"], linewidth=1.3, alpha=0.9)
        ax.axvline(min(end, duration), color=COLORS["anomaly"], linewidth=1.3, alpha=0.9)
        ax.text(
            max(start, view_lo), ax.get_ylim()[1] * 0.92,
            localized_text(" 证据窗口", " evidence window"),
            fontproperties=human_font_properties(size=9), color=COLORS["anomaly"], va="top",
        )

    _title(ax, title, subtitle, "PCM Waveform")
    _axis(ax, xlabel_zh="时间（s）", xlabel_en="Time (s)", ylabel_zh="PCM 归一化幅度", ylabel_en="Normalized PCM amplitude")
    return _save_png(fig)


def render_human_spectrogram_png(
    spectrogram: dict,
    *,
    anomaly_start: float | None = None,
    anomaly_end: float | None = None,
    display_start: float | None = None,
    display_end: float | None = None,
    title: str = "PCM Spectrogram",
    subtitle: str | None = None,
    max_frequency_hz: float | None = None,
    dynamic_range_db: float = 80.0,
    width_px: int = 1800,
    height_px: int = 760,
) -> bytes:
    if not human_cjk_font_available():
        return _fallback_spectrogram(
            spectrogram,
            anomaly_start=anomaly_start,
            anomaly_end=anomaly_end,
            display_start=display_start,
            display_end=display_end,
            title=title,
            subtitle=subtitle,
            max_frequency_hz=max_frequency_hz,
            dynamic_range_db=dynamic_range_db,
            width_px=width_px,
            height_px=height_px,
        )

    times = np.asarray(spectrogram.get("times") or [], dtype=float)
    freqs = np.asarray(spectrogram.get("frequencies") or [], dtype=float)
    raw = np.asarray(spectrogram.get("db") or [], dtype=float)
    fig, ax = plt.subplots(figsize=(width_px / 160.0, height_px / 160.0), constrained_layout=True)
    fig.patch.set_facecolor(COLORS["background"])
    ax.set_facecolor(COLORS["panel"])

    image = None
    if raw.ndim == 2 and raw.size and times.size and freqs.size:
        relative = raw - float(np.nanmax(raw))
        max_freq = float(max_frequency_hz or min(4000.0, float(freqs[-1])))
        mask = freqs <= max_freq
        freq_view = freqs[mask]
        data = relative[:, mask].T
        if freq_view.size:
            image = ax.imshow(
                data, origin="lower", aspect="auto",
                extent=[float(times[0]), float(times[-1]), float(freq_view[0]), float(freq_view[-1])],
                cmap="magma", vmin=-abs(float(dynamic_range_db)), vmax=0.0, interpolation="nearest",
            )
            ax.set_ylim(0.0, max_freq)
            view_lo, view_hi, _ = _auto_focus(float(times[-1]), anomaly_start, anomaly_end, display_start, display_end)
            view_lo = max(float(times[0]), view_lo)
            view_hi = min(float(times[-1]), view_hi)
            ax.set_xlim(view_lo, max(view_hi, view_lo + 1e-6))
    if image is None:
        ax.text(
            0.5, 0.5, localized_text("无时频数据", "No spectrogram data"),
            transform=ax.transAxes, ha="center", va="center",
            fontproperties=human_font_properties(size=10), color=COLORS["muted"],
        )

    if anomaly_start is not None:
        start = max(0.0, float(anomaly_start))
        end = max(start, float(anomaly_end if anomaly_end is not None else anomaly_start))
        if end <= start:
            end = start + 0.02
        ax.axvspan(start, end, color=COLORS["anomaly"], alpha=0.12)
        ax.axvline(start, color=COLORS["anomaly"], linewidth=1.2)
        ax.axvline(end, color=COLORS["anomaly"], linewidth=1.2)

    _title(ax, title, subtitle, "PCM Spectrogram")
    _axis(ax, xlabel_zh="时间（s）", xlabel_en="Time (s)", ylabel_zh="频率（Hz）", ylabel_en="Frequency (Hz)")
    if image is not None:
        cbar = fig.colorbar(image, ax=ax, pad=0.015, fraction=0.025)
        cbar.set_label(localized_text("相对电平（dB）", "Relative level (dB)"), fontproperties=human_font_properties(size=9))
        cbar.ax.tick_params(labelsize=8)
    return _save_png(fig)


def render_human_spectrum_png_from_wav(
    wav_bytes: bytes,
    *,
    canonical_spectral: dict | None = None,
    reference_frequencies_hz=None,
    title: str = "PCM Spectrum",
    subtitle: str | None = None,
    min_frequency_hz: float = 30.0,
    max_frequency_hz: float = 3800.0,
    max_seconds: float = 30.0,
    width_px: int = 1800,
    height_px: int = 760,
) -> tuple[bytes, dict]:
    if not human_cjk_font_available():
        png, meta = _fallback_spectrum(
            wav_bytes,
            canonical_spectral=canonical_spectral,
            reference_frequencies_hz=reference_frequencies_hz,
            title=title,
            subtitle=subtitle,
            min_frequency_hz=min_frequency_hz,
            max_frequency_hz=max_frequency_hz,
            max_seconds=max_seconds,
            width_px=width_px,
            height_px=height_px,
        )
        return png, {**meta, "presentation_font": human_font_status()}

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
            ax.get_xaxis().set_major_formatter(ScalarFormatter())
    else:
        ax.text(
            0.5, 0.5, localized_text("无频谱数据", "No spectrum data"),
            transform=ax.transAxes, ha="center", va="center",
            fontproperties=human_font_properties(size=10), color=COLORS["muted"],
        )

    refs = [float(x) for x in (reference_frequencies_hz or []) if min_frequency_hz <= float(x) <= upper]
    for ref in refs:
        ax.axvline(ref, color=COLORS["reference"], linewidth=0.8, alpha=0.45)

    for peak in list((canonical_spectral or {}).get("peaks") or [])[:10]:
        try:
            pf = float(peak.get("frequency_hz"))
        except (TypeError, ValueError):
            continue
        if not (min_frequency_hz <= pf <= upper) or not np.any(valid):
            continue
        index = int(np.argmin(np.abs(freqs - pf)))
        py = float(dbfs[index])
        ax.scatter([pf], [py], s=28, color=COLORS["spectrum"], zorder=4)
        ax.annotate(f"{pf:.1f} Hz", (pf, py), xytext=(5, 8), textcoords="offset points", fontsize=8.5, color=COLORS["text"])

    _title(ax, title, subtitle, "PCM Spectrum")
    _axis(ax, xlabel_zh="频率（Hz，对数刻度）", xlabel_en="Frequency (Hz, log scale)", ylabel_zh="频谱电平（dBFS）", ylabel_en="Spectrum level (dBFS)")
    png = _save_png(fig)
    return png, {
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
        "presentation_font": human_font_status(),
    }


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
    if not human_cjk_font_available():
        png, meta = _fallback_wav_spectrogram(
            wav_bytes,
            start_seconds=start_seconds,
            end_seconds=end_seconds,
            max_frequency_hz=max_frequency_hz,
            n_fft=n_fft,
            hop=hop,
            dynamic_range_db=dynamic_range_db,
            reference_frequencies_hz=reference_frequencies_hz,
            title=title,
            subtitle=subtitle,
            width_px=width_px,
            height_px=height_px,
        )
        return png, {**meta, "presentation_font": human_font_status()}

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
    if n_fft % 2:
        n_fft -= 1
    hop = int(max(1, min(hop, max(1, n_fft - 1))))
    window = np.hanning(n_fft)
    rows = []
    times = []
    if x.size >= n_fft:
        for start in range(0, x.size - n_fft + 1, hop):
            chunk = x[start:start + n_fft]
            magnitude = np.abs(np.fft.rfft(chunk * window))
            rows.append(20.0 * np.log10(np.maximum(magnitude, 1e-12)))
            times.append(lo + (start + n_fft / 2) / sample_rate)
    elif x.size:
        padded = np.zeros(n_fft, dtype=float)
        padded[:x.size] = x
        rows.append(20.0 * np.log10(np.maximum(np.abs(np.fft.rfft(padded * window)), 1e-12)))
        times.append(lo + (x.size / 2) / sample_rate)

    fig, ax = plt.subplots(figsize=(width_px / 160.0, height_px / 160.0), constrained_layout=True)
    fig.patch.set_facecolor(COLORS["background"])
    ax.set_facecolor(COLORS["panel"])
    image = None
    frequencies = np.fft.rfftfreq(n_fft, d=1.0 / sample_rate)
    upper = min(float(max_frequency_hz), sample_rate / 2.0)
    mask = frequencies <= upper
    if rows and np.any(mask):
        values = np.asarray(rows, dtype=float).T
        relative = values - float(np.nanmax(values))
        visible = relative[mask, :]
        time_values = np.asarray(times, dtype=float)
        left = float(time_values[0] - (hop / (2 * sample_rate))) if len(time_values) > 1 else lo
        right = float(time_values[-1] + (hop / (2 * sample_rate))) if len(time_values) > 1 else hi
        image = ax.imshow(
            visible, origin="lower", aspect="auto",
            extent=[left, right, float(frequencies[mask][0]), float(frequencies[mask][-1])],
            cmap="magma", vmin=-abs(float(dynamic_range_db)), vmax=0.0, interpolation="bilinear",
        )
        ax.set_xlim(lo, max(hi, lo + 1e-6))
        ax.set_ylim(0.0, upper)
        for ref in reference_frequencies_hz or ():
            value = float(ref)
            if 0.0 <= value <= upper:
                ax.axhline(value, color=COLORS["reference"], linewidth=0.55, alpha=0.25)
    else:
        ax.text(
            0.5, 0.5, localized_text("无时频数据", "No spectrogram data"),
            transform=ax.transAxes, ha="center", va="center",
            fontproperties=human_font_properties(size=10), color=COLORS["muted"],
        )

    _title(ax, title, subtitle, "PCM High Resolution Spectrogram")
    _axis(ax, xlabel_zh="证据片段内时间（s）", xlabel_en="Time inside evidence clip (s)", ylabel_zh="频率（Hz）", ylabel_en="Frequency (Hz)")
    if image is not None:
        cbar = fig.colorbar(image, ax=ax, pad=0.015, fraction=0.025)
        cbar.set_label(localized_text("相对电平（dB）", "Relative level (dB)"), fontproperties=human_font_properties(size=9))
        cbar.ax.tick_params(labelsize=8)

    meta = {
        "measurement_method": "NUMPY_STFT_HANN_RELATIVE_DB_V1",
        "sample_rate": sample_rate,
        "channels": channels,
        "n_fft": n_fft,
        "hop": hop,
        "time_window_seconds": [round(lo, 6), round(hi, 6)],
        "frequency_range_hz": [0.0, float(upper)],
        "level_unit": "relative dB",
        "absolute_dbfs": False,
        "dynamic_range_db": float(dynamic_range_db),
        "presentation_font": human_font_status(),
    }
    return _save_png(fig), meta
