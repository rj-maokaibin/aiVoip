from __future__ import annotations

import os
import tempfile
from pathlib import Path


def _configure_matplotlib_cache() -> None:
    """Point Matplotlib at a writable cache before any renderer imports it."""
    if os.environ.get("MPLCONFIGDIR"):
        return
    path = Path(tempfile.gettempdir()) / "voip-ai-matplotlib"
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    os.environ["MPLCONFIGDIR"] = str(path)


_configure_matplotlib_cache()

from .cross_layer import render_human_cross_layer_png
from .dtmf_inspector import measure_dtmf_event, render_human_dtmf_inspector_png
from .explanations import build_human_explanation
from .localized_renderers import (
    render_human_spectrum_png_from_wav,
    render_human_spectrogram_png,
    render_human_spectrogram_png_from_wav,
    render_human_waveform_png,
)
from .multitrack import render_human_multitrack_png
from .rtp_timeline import render_human_rtp_timeline_png
from .theme import (
    HUMAN_RENDERER_VERSION,
    PRESENTATION_PROFILE,
    human_renderer_enabled,
    human_feishu_preferred,
)
from .typography import (
    human_cjk_font_available,
    human_font_status,
    reset_cjk_font_cache,
)

__all__ = [
    "HUMAN_RENDERER_VERSION",
    "PRESENTATION_PROFILE",
    "build_human_explanation",
    "human_renderer_enabled",
    "human_feishu_preferred",
    "human_cjk_font_available",
    "human_font_status",
    "reset_cjk_font_cache",
    "measure_dtmf_event",
    "render_human_dtmf_inspector_png",
    "render_human_cross_layer_png",
    "render_human_multitrack_png",
    "render_human_rtp_timeline_png",
    "render_human_spectrum_png_from_wav",
    "render_human_spectrogram_png",
    "render_human_spectrogram_png_from_wav",
    "render_human_waveform_png",
]
