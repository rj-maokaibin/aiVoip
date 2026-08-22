from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


EVIDENCE_CARD_VERSION = "evidence-card-v1"
_IMAGE_TYPES = {"WAVEFORM_PNG", "SPECTRUM_PNG", "SPECTROGRAM_PNG", "RTP_TIMELINE_PNG", "SIP_CALL_FLOW_PNG"}
_AUDIO_TYPES = {"AUDIO_CLIP", "PERIODIC_AUDIO_CLIP"}
_AUDIO_EXPECTED_FINDINGS = {
    "PACKET_LOSS", "BURST_LOSS", "HIGH_DELTA", "PCM_GAP", "UNEXPECTED_SILENCE", "CLICK_POP",
    "PERIODIC_LOW_FREQUENCY_INTERFERENCE", "LOCAL_CAPTURE_PERIODIC_INTERFERENCE", "ECHO_PATH_DETECTED", "DTMF_ABNORMAL",
}


def _epoch(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    raw = str(value).strip()
    try:
        return float(raw)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _iso_utc(value: Any) -> str | None:
    epoch = _epoch(value)
    if epoch is None:
        return str(value) if value is not None else None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _call_start(call: dict | None) -> float | None:
    for key in ("started_at", "media_start_time", "start_time"):
        value = _epoch((call or {}).get(key))
        if value is not None:
            return value
    return None


def _relative(value: Any, call_start: float | None) -> str | None:
    epoch = _epoch(value)
    if epoch is None or call_start is None:
        return None
    delta = epoch - call_start
    return f"T{'+' if delta >= 0 else '-'}{abs(delta):.3f}s"


def _time_display(finding: dict, call: dict | None) -> dict:
    tr = finding.get("time_range") or {}
    call_start = _call_start(call)
    start, end, rep = tr.get("start"), tr.get("end"), tr.get("representative")
    return {
        "absolute_start_utc": _iso_utc(start), "absolute_end_utc": _iso_utc(end), "representative_utc": _iso_utc(rep),
        "call_relative_start": _relative(start, call_start), "call_relative_end": _relative(end, call_start),
        "call_relative_representative": _relative(rep, call_start),
        "clock_boundary": "Absolute time is rendered in UTC; T+ is relative to the reconstructed/runtime Call start when available.",
    }


def _scope_display(finding: dict) -> dict:
    scope = finding.get("scope") or {}
    return {
        "layer": scope.get("layer") or scope.get("pcm_tap") or "UNKNOWN",
        "call_id": scope.get("call_id"), "rtp_stream_id": scope.get("rtp_stream_id"),
        "direction": scope.get("direction") or scope.get("rtp_direction") or scope.get("pcm_direction"),
        "pcm_tap": scope.get("pcm_tap"), "ssrc": scope.get("ssrc"), "call_direction_role": scope.get("call_direction_role"),
    }


def _add_measurement(out: list[dict], label: str, value: Any, unit: str | None = None, meaning: str | None = None) -> None:
    if value is not None:
        out.append({"label": label, "value": value, "unit": unit, "meaning": meaning})


def _periodic_ac20(result: dict | None) -> Any:
    return ((((result or {}).get("representative") or {}).get("autocorrelation") or {}).get("20ms"))


def _periodic_comb_hits(result: dict | None) -> Any:
    return (((result or {}).get("comb") or {}).get("hit_count"))


def _measurements(finding: dict) -> list[dict]:
    metrics = finding.get("metrics") or {}
    ftype = str(finding.get("type") or "")
    out: list[dict] = []
    if ftype == "HIGH_DELTA":
        _add_measurement(out, "异常事件数", metrics.get("event_count") or finding.get("occurrence_count"), "events")
        _add_measurement(out, "最大 RTP 包间隔", metrics.get("max_delta_ms") or metrics.get("stream_max_delta_ms"), "ms")
        _add_measurement(out, "预期 ptime", metrics.get("ptime_ms") or metrics.get("expected_ptime_ms"), "ms")
        _add_measurement(out, "最大超额延迟", metrics.get("max_excess_delay_ms"), "ms")
        _add_measurement(out, "RTP 丢包数", metrics.get("stream_lost_packets"), "packets", "Sequence 连续时 HIGH_DELTA 不等于 Packet Loss。")
        _add_measurement(out, "全部事件 Sequence 连续", metrics.get("all_sequence_continuous"))
    elif ftype in {"LOCAL_CAPTURE_PERIODIC_INTERFERENCE", "PERIODIC_INTERFERENCE_PATH_COMPARISON"} and metrics.get("pcm_rx"):
        pcm_rx, upstream, downstream = metrics.get("pcm_rx") or {}, metrics.get("upstream_rtp") or {}, metrics.get("downstream_rtp") or {}
        strength = metrics.get("strength") or {}
        _add_measurement(out, "PCM_RX 周期等级", pcm_rx.get("level"))
        _add_measurement(out, "PCM_RX 20ms 自相关", _periodic_ac20(pcm_rx))
        _add_measurement(out, "PCM_RX 频梳命中", _periodic_comb_hits(pcm_rx), "peaks")
        _add_measurement(out, "PCM_RX 周期强度", strength.get("pcm_rx"))
        _add_measurement(out, "上行 RTP 周期强度", strength.get("upstream_rtp"))
        _add_measurement(out, "反向 RTP 周期强度", strength.get("downstream_rtp"))
        _add_measurement(out, "上行 RTP 20ms 自相关", _periodic_ac20(upstream))
        _add_measurement(out, "反向 RTP 20ms 自相关", _periodic_ac20(downstream))
    elif ftype == "PERIODIC_LOW_FREQUENCY_INTERFERENCE":
        hum = metrics.get("hum") or metrics
        _add_measurement(out, "主周期/工频族", hum.get("dominant_family"))
        _add_measurement(out, "周期特征得分", hum.get("score") or metrics.get("periodicity_strength"))
        _add_measurement(out, "20ms 自相关", hum.get("ac20") or metrics.get("ac20"))
        _add_measurement(out, "频梳命中", hum.get("comb_hits") or metrics.get("comb_hits"), "peaks")
    elif ftype == "UNEXPECTED_SILENCE":
        _add_measurement(out, "静音持续", metrics.get("duration_ms"), "ms")
        _add_measurement(out, "静音阈值", metrics.get("threshold_dbfs"), "dBFS")
        _add_measurement(out, "跨层匹配", metrics.get("cross_layer_status") or metrics.get("reason_code"))
    elif ftype == "CLICK_POP":
        _add_measurement(out, "突变量", metrics.get("jump"))
        _add_measurement(out, "短时能量抬升", metrics.get("energy_rise_db"), "dB")
        _add_measurement(out, "高频能量比例", metrics.get("highband_ratio"))
        _add_measurement(out, "候选置信度", metrics.get("confidence"))
    elif ftype in {"PACKET_LOSS", "BURST_LOSS"}:
        _add_measurement(out, "丢包数", metrics.get("lost_packets"), "packets")
        _add_measurement(out, "估算音频缺口", metrics.get("estimated_audio_loss_ms"), "ms")
        _add_measurement(out, "ptime", metrics.get("ptime_ms"), "ms")
    elif ftype.startswith("DTMF"):
        _add_measurement(out, "PCM 数字", metrics.get("pcm_digits") or metrics.get("digits"))
        _add_measurement(out, "SIP 目标", metrics.get("sip_target"))
        _add_measurement(out, "匹配结果", metrics.get("match") or metrics.get("status"))
    elif ftype == "ECHO_PATH_DETECTED":
        _add_measurement(out, "估算回声时延", metrics.get("delay_ms"), "ms")
        _add_measurement(out, "相关系数", metrics.get("absolute_correlation") or metrics.get("correlation"))
    if not out:
        for key, value in metrics.items():
            if isinstance(value, (str, int, float, bool)) and len(out) < 6:
                _add_measurement(out, key, value)
    return out[:8]


def _packet_refs(finding: dict) -> list[dict]:
    metrics = finding.get("metrics") or {}
    refs: list[dict] = []
    events = metrics.get("events") or []
    if isinstance(events, list):
        for index, event in enumerate(events[:8], start=1):
            if not isinstance(event, dict):
                continue
            if any(event.get(k) is not None for k in ("previous_frame_number", "current_frame_number", "previous_sequence", "current_sequence")):
                refs.append({
                    "event": index, "time": event.get("time"), "previous_frame": event.get("previous_frame_number"),
                    "current_frame": event.get("current_frame_number"), "previous_seq": event.get("previous_sequence"),
                    "current_seq": event.get("current_sequence"), "delta_ms": event.get("delta_ms"), "classification": event.get("classification"),
                })
    if not refs and any(metrics.get(k) is not None for k in ("previous_frame_number", "current_frame_number", "previous_sequence", "current_sequence")):
        refs.append({
            "event": 1, "previous_frame": metrics.get("previous_frame_number"), "current_frame": metrics.get("current_frame_number"),
            "previous_seq": metrics.get("previous_sequence"), "current_seq": metrics.get("current_sequence"),
            "delta_ms": metrics.get("delta_ms"), "classification": metrics.get("classification"),
        })
    return refs


def _artifact_kind(ref: dict) -> str:
    """Classify only explicit report-safe Artifact types; MIME is not an authority boundary."""
    atype = str(ref.get("type") or "").upper()
    if atype in _IMAGE_TYPES:
        return "IMAGE"
    if atype in _AUDIO_TYPES:
        return "AUDIO"
    return "DETAIL"


def _artifact_priority(ftype: str, ref: dict) -> tuple[int, str]:
    atype = str(ref.get("type") or "").upper()
    preferred = {
        "HIGH_DELTA": ["RTP_TIMELINE_PNG", "WAVEFORM_PNG", "SPECTROGRAM_PNG", "AUDIO_CLIP"],
        "PACKET_LOSS": ["RTP_TIMELINE_PNG", "WAVEFORM_PNG", "AUDIO_CLIP"],
        "BURST_LOSS": ["RTP_TIMELINE_PNG", "WAVEFORM_PNG", "AUDIO_CLIP"],
        "LOCAL_CAPTURE_PERIODIC_INTERFERENCE": ["SPECTRUM_PNG", "SPECTROGRAM_PNG", "WAVEFORM_PNG", "PERIODIC_AUDIO_CLIP"],
        "PERIODIC_LOW_FREQUENCY_INTERFERENCE": ["SPECTRUM_PNG", "SPECTROGRAM_PNG", "WAVEFORM_PNG", "PERIODIC_AUDIO_CLIP"],
        "UNEXPECTED_SILENCE": ["WAVEFORM_PNG", "SPECTROGRAM_PNG", "AUDIO_CLIP"],
        "CLICK_POP": ["WAVEFORM_PNG", "SPECTROGRAM_PNG", "AUDIO_CLIP"],
        "SIP_CALL_FAILED": ["SIP_CALL_FLOW_PNG"], "CODEC_NEGOTIATION_MISMATCH": ["SIP_CALL_FLOW_PNG", "RTP_TIMELINE_PNG"],
    }
    order = preferred.get(ftype, ["WAVEFORM_PNG", "SPECTRUM_PNG", "SPECTROGRAM_PNG", "RTP_TIMELINE_PNG", "SIP_CALL_FLOW_PNG", "AUDIO_CLIP", "PERIODIC_AUDIO_CLIP"])
    try:
        rank = order.index(atype)
    except ValueError:
        rank = 50
    return rank, str(ref.get("filename") or "")


def _artifact_display(ref: dict) -> dict:
    meta = ref.get("metadata") or {}
    annotation = meta.get("annotation_contract") or {}
    artifact_id = ref.get("artifact_id")
    kind = _artifact_kind(ref)
    source = meta.get("source")
    source_direction = source.get("direction") if isinstance(source, dict) else None
    return {
        "artifact_id": artifact_id, "type": ref.get("type"), "filename": ref.get("filename"),
        "content_type": ref.get("content_type"), "role": ref.get("role"), "kind": kind,
        "content_url": f"/api/v1/artifacts/{artifact_id}/content" if artifact_id and kind in {"IMAGE", "AUDIO"} else None,
        "caption": annotation.get("caption") or annotation.get("title") or ref.get("filename"),
        "source": source if source is not None else {},
        "time_window": meta.get("time_window") or meta.get("anomaly_window") or {},
        "direction": meta.get("direction") or source_direction,
        "annotation_complete": bool(meta.get("annotation_complete")), "annotation_contract": annotation,
    }


def _next_action(ftype: str) -> str:
    return {
        "HIGH_DELTA": "对齐同时间 PCM RX/TX Gap、反向 RTP 和 DUT 运行日志；只有跨层同步证据才能继续收窄发送端/网络/抓包观察点边界。",
        "PACKET_LOSS": "下钻丢包边界 Frame/Seq，并对照对端或另一观察点抓包，区分发送端未发、链路丢失和接收侧未捕获。",
        "BURST_LOSS": "复核突发丢包前后 RTP Timeline、网络队列/接口计数和对端抓包，确认丢失发生在哪个观察边界。",
        "LOCAL_CAPTURE_PERIODIC_INTERFERENCE": "使用异常 Clip 与 Spectrum/Spectrogram 做 A/B：话柄、FXS/SLIC、供电/接地逐项替换；比较 PCM_RX 与 RTP_UP 特征是否同步消失。",
        "PERIODIC_LOW_FREQUENCY_INTERFERENCE": "选择低能量代表窗复核频谱与音频；如需确认物理来源，执行话柄/线路/FXS-SLIC/供电接地 A/B。",
        "UNEXPECTED_SILENCE": "试听异常窗并同时对照对应方向 RTP 与 PCM；只有上游有能量而下游静音时才继续收窄静音引入层。",
        "CLICK_POP": "先试听 Clip，并确认异常窗不与 DTMF/拨号音/媒体边界重叠；再用相邻层波形判断瞬态首次出现位置。",
        "ECHO_PATH_DETECTED": "复核参考/观测 Tap 的相关峰和时延，并用端点静音或受控单向语音验证回声路径。",
        "DTMF_ABNORMAL": "对齐 PCM DTMF、SIP 目标号码和逐位时序，确认异常是信号质量、时序还是实际号码不一致。",
    }.get(ftype, "复核该 Finding 的代表时间窗、原始 Evidence 和相邻层对照，再决定是否进入确定性 Diagnosis/A-B/Fix Verification。")


def build_evidence_card(finding: dict, *, call: dict | None = None) -> dict:
    ftype = str(finding.get("type") or "")
    refs = [_artifact_display(ref) for ref in (finding.get("artifact_refs") or [])]
    refs.sort(key=lambda ref: _artifact_priority(ftype, ref))
    visuals = [x for x in refs if x["kind"] == "IMAGE"][:3]
    audio = [x for x in refs if x["kind"] == "AUDIO"][:3]
    details = [x for x in refs if x["kind"] == "DETAIL"][:6]
    audio_expected = ftype in _AUDIO_EXPECTED_FINDINGS
    audio_status = "AVAILABLE" if audio else "UNAVAILABLE" if audio_expected else "NOT_REQUIRED"
    audio_reason = None if audio_status == "AVAILABLE" else (
        "NO_MATCHING_ANOMALY_AUDIO_CLIP: 当前 Finding 未关联到可安全展示的代表异常音频；不得用其他时间窗/其他 Finding 的音频替代。"
        if audio_status == "UNAVAILABLE" else "This Finding type does not require an anomaly audio clip."
    )
    return {
        "version": EVIDENCE_CARD_VERSION, "finding_id": finding.get("finding_id"), "finding_type": ftype,
        "severity": finding.get("severity"), "title": finding.get("title"), "what_happened": finding.get("observation"),
        "initial_interpretation": finding.get("interpretation"), "scope": _scope_display(finding), "time": _time_display(finding, call),
        "measurements": _measurements(finding), "packet_refs": _packet_refs(finding), "visual_evidence": visuals,
        "audio_evidence": {"status": audio_status, "reason": audio_reason, "clips": audio}, "detail_artifacts": details,
        "root_cause_boundary": finding.get("root_cause_boundary"), "next_action": _next_action(ftype),
        "traceability": {
            "evidence_refs": finding.get("evidence_refs") or [], "event_refs": finding.get("event_refs") or [],
            "source_analyzer_run_ids": finding.get("source_analyzer_run_ids") or [], "artifact_count": len(refs),
        },
    }


def attach_evidence_cards(payload: dict) -> dict:
    call = payload.get("display_call") or payload.get("call") or {}
    cards = []
    for finding in payload.get("findings") or []:
        card = build_evidence_card(finding, call=call)
        finding["evidence_card"] = card
        cards.append(card)
    payload["evidence_cards"] = cards
    payload["evidence_card_summary"] = {
        "version": EVIDENCE_CARD_VERSION, "finding_count": len(cards),
        "audio_expected_count": sum(1 for c in cards if c["audio_evidence"]["status"] != "NOT_REQUIRED"),
        "audio_available_count": sum(1 for c in cards if c["audio_evidence"]["status"] == "AVAILABLE"),
        "audio_unavailable_count": sum(1 for c in cards if c["audio_evidence"]["status"] == "UNAVAILABLE"),
        "cards_with_visuals": sum(1 for c in cards if c["visual_evidence"]),
        "cards_with_packet_refs": sum(1 for c in cards if c["packet_refs"]),
    }
    return payload