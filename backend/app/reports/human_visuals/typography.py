from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from matplotlib import font_manager, ft2font
from matplotlib.font_manager import FontProperties

# Human Renderer only. Machine Evidence deliberately stays independent from
# system fonts for deterministic Golden/Audit rendering.
_CJK_PROBE = "中文频率波形时频图证据窗口"
_PREFERRED_PATTERNS = (
    "notosanscjk-regular",
    "notosanscjk",
    "sourcehansanscn",
    "sourcehansanssc",
    "wqy-microhei",
    "wenquanyi micro hei",
    "wqy-zenhei",
    "simhei",
    "msyh",
)


def _has_cjk_glyphs(path: str) -> bool:
    try:
        charmap = ft2font.FT2Font(path).get_charmap()
    except Exception:
        return False
    return all(ord(ch) in charmap for ch in _CJK_PROBE)


def _score(path: str) -> tuple[int, str]:
    normalized = str(path).lower().replace("_", "").replace(" ", "")
    for index, pattern in enumerate(_PREFERRED_PATTERNS):
        token = pattern.lower().replace("_", "").replace(" ", "")
        if token in normalized:
            return index, normalized
    return len(_PREFERRED_PATTERNS), normalized


@lru_cache(maxsize=1)
def resolve_cjk_font() -> dict:
    """Resolve a system CJK font without bundling font files in the repository.

    An explicit HUMAN_EVIDENCE_CJK_FONT_PATH wins when it exists and contains
    the required Simplified-Chinese glyphs. Otherwise system fonts are scanned
    deterministically. Missing CJK fonts are a presentation degradation only;
    callers must fall back to English rather than failing report generation.
    """
    explicit = str(os.getenv("HUMAN_EVIDENCE_CJK_FONT_PATH") or "").strip()
    if explicit:
        candidate = Path(explicit)
        if candidate.is_file() and _has_cjk_glyphs(str(candidate)):
            try:
                family = ft2font.FT2Font(str(candidate)).family_name
            except Exception:
                family = candidate.stem
            return {
                "cjk_available": True,
                "font_path": str(candidate),
                "font_family": family,
                "source": "ENV",
            }

    paths = sorted(
        set(font_manager.findSystemFonts(fontext="ttf") + font_manager.findSystemFonts(fontext="otf")),
        key=_score,
    )
    for path in paths:
        score, _ = _score(path)
        if score >= len(_PREFERRED_PATTERNS):
            continue
        if not _has_cjk_glyphs(path):
            continue
        try:
            family = ft2font.FT2Font(path).family_name
        except Exception:
            family = Path(path).stem
        return {
            "cjk_available": True,
            "font_path": path,
            "font_family": family,
            "source": "SYSTEM",
        }

    return {
        "cjk_available": False,
        "font_path": None,
        "font_family": "DejaVu Sans",
        "source": "FALLBACK",
        "reason": "CJK_FONT_UNAVAILABLE",
    }


def reset_cjk_font_cache() -> None:
    resolve_cjk_font.cache_clear()


def human_cjk_font_available() -> bool:
    return bool(resolve_cjk_font().get("cjk_available"))


def human_font_properties(*, size: float | None = None, weight: str | None = None) -> FontProperties:
    status = resolve_cjk_font()
    kwargs = {}
    if size is not None:
        kwargs["size"] = size
    if weight is not None:
        kwargs["weight"] = weight
    if status.get("cjk_available") and status.get("font_path"):
        return FontProperties(fname=str(status["font_path"]), **kwargs)
    return FontProperties(family="DejaVu Sans", **kwargs)


def human_font_status() -> dict:
    status = dict(resolve_cjk_font())
    # Font path is operational detail and should not be projected into user-facing
    # Feishu text; metadata only needs availability/family/source.
    status.pop("font_path", None)
    return status


def localized_text(chinese: str, english: str) -> str:
    return chinese if human_cjk_font_available() else english


def localized_title(value: str | None, fallback_english: str) -> str:
    raw = str(value or "").strip()
    if human_cjk_font_available():
        text = raw or fallback_english
        replacements = (
            ("High Resolution Spectrogram", "高分辨率时频图"),
            ("Continuous Spectrum", "连续频谱"),
            ("Periodic Interference", "周期性干扰"),
            ("Spectrogram", "时频图"),
            ("Waveform", "波形"),
            ("Spectrum", "频谱"),
        )
        for old, new in replacements:
            text = text.replace(old, new)
        return text

    clean = "".join(ch if 32 <= ord(ch) <= 126 else " " for ch in raw)
    clean = " ".join(clean.split()).strip(" -|.")
    return clean or fallback_english
