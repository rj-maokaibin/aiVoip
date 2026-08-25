from __future__ import annotations

import os

HUMAN_RENDERER_VERSION = "human-evidence-renderer-v2"
PRESENTATION_PROFILE = "AUDACITY_INSPIRED_V1"

# Human-facing visual palette. Machine Evidence continues to use the frozen
# deterministic renderer; these values are presentation-only and have no
# diagnostic authority.
COLORS = {
    "background": "#FFFFFF",
    "panel": "#F8FAFC",
    "grid": "#D7DEE8",
    "text": "#172033",
    "muted": "#667085",
    "waveform": "#2367A7",
    "waveform_fill": "#7FB3E1",
    "rms": "#B86A33",
    "spectrum": "#6D3FB5",
    "spectrum_fill": "#B39DDB",
    "reference": "#D39132",
    "anomaly": "#C43D3D",
    "success": "#2E7D5A",
}


def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() not in {"0", "false", "no", "off", "disabled"}


def human_renderer_enabled() -> bool:
    """Deployment feature flag without changing the frozen Settings contract.

    The first Human Renderer release stays additive. A deployment can disable it
    immediately with HUMAN_EVIDENCE_RENDERER_ENABLED=false while Machine Evidence
    remains authoritative and available.
    """
    return _flag("HUMAN_EVIDENCE_RENDERER_ENABLED", True)


def human_feishu_preferred() -> bool:
    return _flag("HUMAN_EVIDENCE_FEISHU_PREFERRED", True)
