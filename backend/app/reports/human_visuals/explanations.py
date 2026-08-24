from __future__ import annotations

from typing import Any


_LOOK_AT = {
    "WAVEFORM": "这张图用于查看音频波形随时间的变化，并定位异常时间窗内是否出现突变、静音、削顶或明显能量变化。横轴是时间，纵轴是归一化 PCM 幅度。",
    "SPECTRUM": "这张图用于查看当前音频窗口中各频率成分的强弱。横轴是频率 Hz，纵轴是相对于数字满量程的频谱电平 dBFS；标记线用于对应 Analyzer 已识别的主峰或参考频率。",
    "SPECTROGRAM": "这张图用于查看不同频率的能量如何随时间变化。横轴是时间，纵轴是频率，颜色越亮表示该时间和频率位置的相对能量越强。",
    "RTP_TIMELINE": "这张图用于查看 RTP 媒体包在同一时间轴上的异常事件，例如延迟突增、丢包或突发丢包；网络事件必须与其 Canonical 类型保持一致。",
}


def _clean(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    return text or None


def build_human_explanation(finding: Any, visual_kind: str) -> dict:
    """Build a plain-language explanation strictly from canonical Finding facts.

    This helper intentionally does not infer a new root cause or re-run any
    detector. It projects the existing observation / interpretation / boundary
    into a fixed Human Visual explanation contract.
    """
    kind = str(visual_kind or "").upper()
    observation = _clean(getattr(finding, "observation", None))
    interpretation = _clean(getattr(finding, "interpretation", None))
    boundary = _clean(getattr(finding, "root_cause_boundary", None))
    title = _clean(getattr(finding, "title", None))

    observations = [observation] if observation else []
    meaning = interpretation or "该图片用于帮助复核当前 Finding 的已观测证据；它本身不会提升 Evidence Level，也不会独立确认根因。"
    evidence_boundary = boundary or "当前图片仅是现有 Evidence / Analyzer 事实的展示，不可单独用于确认最终 Root Cause。"
    summary = observation or title or "请结合当前 Finding 的测量值、Scope、时间窗和相邻层证据进行复核。"

    return {
        "what_to_look_at": _LOOK_AT.get(kind, "这张图用于帮助复核当前 Finding 的证据事实和异常时间窗。"),
        "observations": observations,
        "meaning": meaning,
        "evidence_boundary": evidence_boundary,
        "plain_language_summary": summary,
        "source_authority": "CANONICAL_FINDING",
        "diagnostic_authority": "NONE",
    }
