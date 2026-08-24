from __future__ import annotations

from typing import Any


_LOOK_AT = {
    "WAVEFORM": "这张图用于查看音频波形随时间的变化，并定位证据窗口内是否出现突变、静音、削顶或明显能量变化。横轴是时间，纵轴是归一化 PCM 幅度。",
    "SPECTRUM": "这张图用于查看当前证据窗口中各频率成分的强弱。横轴是频率 Hz，纵轴是相对于数字满量程的频谱电平 dBFS；标记用于对应 Canonical Analyzer 已识别的主峰或参考频率。",
    "SPECTROGRAM": "这张图用于查看不同频率的能量如何随时间变化。横轴是时间，纵轴是频率，颜色越亮表示该时间和频率位置的相对能量越强。",
    "DTMF_INSPECTOR": "这张图用于检查一个已由现有 DTMF Detector 接受的按键事件：左侧看双音主峰和周边杂散，右侧看理论/实测频率、dBFS、Twist、持续时间以及可用时的 PCM 序列与 SIP 目标对照。",
    "RTP_TIMELINE": "这张图用于查看 RTP 媒体包在同一时间轴上的异常事件，例如延迟突增、丢包或突发丢包；网络事件必须与其 Canonical 类型保持一致。",
    "MULTI_TRACK": "这张图把可用的 PCM RX、RTP Uplink、RTP Downlink、PCM TX 波形放到同一绝对/相对时间轴上，用于直观看同一段媒体在不同层是否同时存在。",
    "CROSS_LAYER": "这张图用于展示现有 Analyzer 已计算的跨层相关性、lag 和可用性，帮助理解证据边界；它不会自行决定物理根因。",
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


def _dtmf_observations(measurement: dict) -> list[str]:
    if str(measurement.get("status") or "").upper() != "MEASURED":
        return []
    digit = str(measurement.get("digit") or "?")
    out = [
        f"按键 {digit} 的低频主音理论值为 {_number(measurement.get('row_expected_hz'), 1)} Hz，实测 {_number(measurement.get('row_measured_hz'), 3)} Hz，频偏 {_number(measurement.get('row_error_percent'), 5)}%。",
        f"按键 {digit} 的高频主音理论值为 {_number(measurement.get('col_expected_hz'), 1)} Hz，实测 {_number(measurement.get('col_measured_hz'), 3)} Hz，频偏 {_number(measurement.get('col_error_percent'), 5)}%。",
    ]
    if measurement.get("row_level_dbfs") is not None and measurement.get("col_level_dbfs") is not None:
        out.append(f"两路主音数字电平分别为 {_number(measurement.get('row_level_dbfs'), 3)} dBFS 和 {_number(measurement.get('col_level_dbfs'), 3)} dBFS，Twist 为 {_number(measurement.get('twist_db'), 3)} dB。")
    if measurement.get("strongest_spur_hz") is not None:
        out.append(f"当前事件窗口最强非主音杂散约为 {_number(measurement.get('strongest_spur_hz'), 3)} Hz / {_number(measurement.get('strongest_spur_dbfs'), 3)} dBFS，Spur Margin 为 {_number(measurement.get('spur_margin_db'), 3)} dB。")
    if measurement.get("duration_ms") is not None:
        out.append(f"该按键事件持续时间约 {_number(measurement.get('duration_ms'), 3)} ms。")
    match = str(measurement.get("sequence_match") or "UNAVAILABLE").upper()
    pcm_sequence = _clean(measurement.get("pcm_sequence"))
    sip_target = _clean(measurement.get("sip_target"))
    if pcm_sequence and sip_target:
        if match == "MATCH":
            out.append(f"PCM 检测序列为 {pcm_sequence}，权威 SIP 目标为 {sip_target}，两者一致。")
        elif match == "MISMATCH":
            out.append(f"PCM 检测序列为 {pcm_sequence}，权威 SIP 目标为 {sip_target}，两者不一致；仅描述跨层事实，不自动推断具体丢号环节。")
    if str(measurement.get("threshold_status") or "").upper() == "UNVERIFIED_THRESHOLD":
        out.append("频偏、最强杂散和 Spur Margin 当前仅作为测量事实展示；未绑定版本化 AnalyzerProfile/Golden 阈值的项目不判 PASS/FAIL。")
    return out


def _cross_layer_observations(measurement: dict) -> list[str]:
    out = []
    for item in measurement.get("correlations") or []:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "跨层对比")
        correlation = _number(item.get("absolute_correlation"), 3)
        lag = _number(item.get("lag_ms"), 3)
        quality = str(item.get("quality") or "UNKNOWN")
        if correlation is not None:
            out.append(f"{label}：相关系数绝对值 {correlation}，lag {lag or 'UNKNOWN'} ms，Analyzer 质量等级 {quality}。")
    boundary = _clean(measurement.get("first_observable_boundary"))
    if boundary:
        out.append(boundary)
    return out


def _measurement_observations(visual_kind: str, measurement: dict | None) -> list[str]:
    if not isinstance(measurement, dict) or not measurement:
        return []
    out: list[str] = []
    kind = str(visual_kind or "").upper()
    if measurement.get("periodic") or any(k in measurement for k in ("period_ms", "comb_hit_count", "harmonics_hz")):
        out.extend(_periodic_observations(measurement))
    if kind == "DTMF_INSPECTOR":
        out.extend(_dtmf_observations(measurement))
    if kind in {"MULTI_TRACK", "CROSS_LAYER"}:
        out.extend(_cross_layer_observations(measurement))
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
