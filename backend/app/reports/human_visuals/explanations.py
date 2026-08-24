from __future__ import annotations

from typing import Any


_LOOK_AT = {
    "WAVEFORM": "这张图用于查看音频波形随时间的变化，并定位证据窗口内是否出现突变、静音、削顶或明显能量变化。横轴是时间，纵轴是归一化 PCM 幅度。",
    "SPECTRUM": "这张图用于查看当前证据窗口中各频率成分的强弱。横轴是频率 Hz，纵轴是相对于数字满量程的频谱电平 dBFS；标记用于对应 Canonical Analyzer 已识别的主峰或参考频率。",
    "SPECTROGRAM": "这张图用于查看不同频率的能量如何随时间变化。横轴是时间，纵轴是频率，颜色越亮表示该时间和频率位置的相对能量越强。",
    "RTP_TIMELINE": "这张图用于查看 RTP 媒体包在同一时间轴上的异常事件，例如延迟突增、丢包或突发丢包；网络事件必须与其 Canonical 类型保持一致。",
}


def _clean(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    return text or None


def _number(value: Any, digits: int = 3) -> str | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{number:.{digits}f}".rstrip("0").rstrip(".")


def _periodic_observations(measurement: dict) -> list[str]:
    periodic = measurement.get("periodic") if isinstance(measurement.get("periodic"), dict) else measurement
    out: list[str] = []
    period_ms = _number(periodic.get("period_ms"), 3)
    frequency_hz = _number(periodic.get("frequency_hz"), 3)
    if period_ms and frequency_hz:
        out.append(f"代表性证据片段中检测到约 {period_ms} ms 的重复周期，对应约 {frequency_hz} Hz。")
    elif period_ms:
        out.append(f"代表性证据片段中检测到约 {period_ms} ms 的重复周期。")
    rms_dbfs = _number(periodic.get("rms_dbfs"), 3)
    if rms_dbfs:
        out.append(f"该代表性片段 RMS 为 {rms_dbfs} dBFS，属于数字音频电平测量。")
    hit_count = periodic.get("comb_hit_count")
    target_count = periodic.get("comb_target_count")
    harmonics = periodic.get("harmonics_hz") or []
    if hit_count is not None:
        hit_text = str(hit_count) if target_count in (None, "") else f"{hit_count}/{target_count}"
        if harmonics:
            freq_text = "/".join(_number(x, 1) or str(x) for x in harmonics[:12])
            out.append(f"50 Hz 相关奇次谐波梳状谱命中 {hit_text}，命中频率包括 {freq_text} Hz。")
        else:
            out.append(f"50 Hz 相关奇次谐波梳状谱命中 {hit_text}。")
    ac = periodic.get("autocorrelation") or {}
    if isinstance(ac, dict) and ac:
        values = []
        for key in ("10ms", "20ms", "40ms"):
            if ac.get(key) is not None:
                values.append(f"{key}={_number(ac.get(key), 3)}")
        if values:
            out.append("自相关测量：" + "，".join(values) + "。")
    return out


def _measurement_observations(visual_kind: str, measurement: dict | None) -> list[str]:
    if not isinstance(measurement, dict) or not measurement:
        return []
    out: list[str] = []
    kind = str(visual_kind or "").upper()
    if measurement.get("periodic") or any(k in measurement for k in ("period_ms", "comb_hit_count", "harmonics_hz")):
        out.extend(_periodic_observations(measurement))
    source_strategy = _clean(measurement.get("evidence_source_strategy"))
    time_window = measurement.get("time_window_seconds")
    if source_strategy == "PERIODIC_AUDIO_CLIP":
        out.append("该图优先使用 Analyzer 已抽取的代表性周期干扰音频片段，而不是整段通话音频。")
    elif source_strategy == "PCM_WAV_FINDING_WINDOW":
        out.append("当前未找到专用代表性片段，图像使用原始 PCM WAV 中与 Finding 对齐的证据窗口生成。")
    if kind == "SPECTROGRAM" and time_window and isinstance(time_window, (list, tuple)) and len(time_window) == 2:
        out.append(f"时频图展示窗口为 {time_window[0]}～{time_window[1]} s；颜色单位为相对 dB，不冒充绝对 dBFS。")
    return out


def build_human_explanation(finding: Any, visual_kind: str, measurement: dict | None = None) -> dict:
    """Build plain-language explanation from Canonical Finding + Human Measurement.

    Measurement facts make the explanation concrete, but never create/upgrade a
    Finding, Evidence Level, PASS/FAIL threshold, or Root Cause conclusion.
    """
    kind = str(visual_kind or "").upper()
    observation = _clean(getattr(finding, "observation", None))
    interpretation = _clean(getattr(finding, "interpretation", None))
    boundary = _clean(getattr(finding, "root_cause_boundary", None))
    title = _clean(getattr(finding, "title", None))

    observations = [observation] if observation else []
    for item in _measurement_observations(kind, measurement):
        if item and item not in observations:
            observations.append(item)
    meaning = interpretation or "该图片用于帮助复核当前 Finding 的已观测证据；它本身不会提升 Evidence Level，也不会独立确认根因。"
    evidence_boundary = boundary or "当前图片仅是现有 Evidence / Analyzer 事实与 Human Measurement 的展示，不可单独用于确认最终 Root Cause。"
    summary = observation or title or "请结合当前 Finding 的测量值、Scope、时间窗和相邻层证据进行复核。"

    return {
        "what_to_look_at": _LOOK_AT.get(kind, "这张图用于帮助复核当前 Finding 的证据事实和异常时间窗。"),
        "observations": observations,
        "meaning": meaning,
        "evidence_boundary": evidence_boundary,
        "plain_language_summary": summary,
        "source_authority": "CANONICAL_FINDING",
        "measurement_authority": "PRESENTATION_FACT_ONLY",
        "diagnostic_authority": "NONE",
    }
