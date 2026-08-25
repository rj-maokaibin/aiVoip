from __future__ import annotations

import io
import wave

import numpy as np

from app.reports import human_visuals
from app.reports.human_visuals import typography


PNG = b"\x89PNG\r\n\x1a\n"


def _wav_bytes(samples: np.ndarray, sample_rate: int = 8000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(samples.astype("<i2").tobytes())
    return buf.getvalue()


def _tone_wav() -> bytes:
    sr = 8000
    t = np.arange(sr, dtype=np.float64) / sr
    x = 9000 * np.sin(2 * np.pi * 150 * t) + 5000 * np.sin(2 * np.pi * 250 * t)
    return _wav_bytes(np.clip(x, -32768, 32767).astype(np.int16), sr)


def test_localized_title_translates_visual_terms_when_cjk_is_available(monkeypatch):
    monkeypatch.setattr(typography, "human_cjk_font_available", lambda: True)
    assert typography.localized_title("周期性音频干扰 · Continuous Spectrum", "PCM Spectrum") == "周期性音频干扰 · 连续频谱"
    assert typography.localized_title("PCM RX · High Resolution Spectrogram", "PCM Spectrogram") == "PCM RX · 高分辨率时频图"


def test_localized_title_keeps_portable_ascii_when_cjk_is_unavailable(monkeypatch):
    monkeypatch.setattr(typography, "human_cjk_font_available", lambda: False)
    assert typography.localized_title("周期性音频干扰 · Continuous Spectrum", "PCM Spectrum") == "Continuous Spectrum"


def test_package_level_spectrum_records_presentation_font_status():
    png, meta = human_visuals.render_human_spectrum_png_from_wav(
        _tone_wav(),
        canonical_spectral={"peaks": [{"frequency_hz": 150.0}, {"frequency_hz": 250.0}]},
        reference_frequencies_hz=[50, 150, 250],
        title="周期性音频干扰 · Continuous Spectrum",
        subtitle="PCM RX · 代表性证据片段 1.0 s",
        max_frequency_hz=1200.0,
    )
    assert png.startswith(PNG)
    assert meta["level_unit"] == "dBFS"
    assert "presentation_font" in meta
    assert isinstance(meta["presentation_font"]["cjk_available"], bool)
    assert meta["presentation_font"]["font_family"]


def test_package_level_wav_spectrogram_records_presentation_font_status():
    png, meta = human_visuals.render_human_spectrogram_png_from_wav(
        _tone_wav(),
        start_seconds=0.0,
        end_seconds=1.0,
        max_frequency_hz=1200.0,
        reference_frequencies_hz=[150, 250, 350],
        title="周期性音频干扰 · High Resolution Spectrogram",
        subtitle="PCM RX · 代表性证据片段 1.0 s",
    )
    assert png.startswith(PNG)
    assert meta["level_unit"] == "relative dB"
    assert "presentation_font" in meta
    assert isinstance(meta["presentation_font"]["cjk_available"], bool)


def test_cjk_font_status_contract_is_safe_for_report_metadata():
    status = human_visuals.human_font_status()
    assert "cjk_available" in status
    assert "font_family" in status
    assert "source" in status
    assert "font_path" not in status
    if not status["cjk_available"]:
        assert status["reason"] == "CJK_FONT_UNAVAILABLE"
