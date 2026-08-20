from __future__ import annotations

from typing import Any


FINDING_EVIDENCE_PACKAGE_VERSION = "finding-evidence-package-v1"

_GRAPH_TYPES = {"WAVEFORM_PNG", "SPECTRUM_PNG", "SPECTROGRAM_PNG", "RTP_TIMELINE_PNG", "SIP_CALL_FLOW_PNG"}
_AUDIO_CLIP_TYPES = {"AUDIO_CLIP", "PERIODIC_AUDIO_CLIP"}
_FULL_AUDIO_TYPES = {"PCM_WAV", "AUDIO_WAV", "RTP_WAV"}
_RTP_TYPES = {"PACKET_LOSS", "BURST_LOSS", "HIGH_DELTA", "PAYLOAD_CHANGE", "ONE_WAY_RTP_MEDIA"}
_SIP_TYPES = {"SIP_REGISTRATION_FAILED", "SIP_CALL_FAILED", "SIP_CONFLICTING_FINAL_RESPONSE", "CODEC_NEGOTIATION_MISMATCH"}
_AUDIBLE_TYPES = {
    "CLICK_POP", "UNEXPECTED_SILENCE", "PERIODIC_LOW_FREQUENCY_INTERFERENCE",
    "LOCAL_CAPTURE_PERIODIC_INTERFERENCE", "ECHO_PATH_DETECTED",
}
_CROSS_LAYER_REQUIRED_TYPES = {"LOCAL_CAPTURE_PERIODIC_INTERFERENCE", "PERIODIC_INTERFERENCE_PATH_COMPARISON", "UNEXPECTED_SILENCE"}


def _atype(ref: dict) -> str:
    return str(ref.get("type") or "").upper()


def _metadata(ref: dict) -> dict:
    return ref.get("metadata") or {}


def _graph_priority(finding_type: str, ref: dict) -> tuple[int, str]:
    atype = _atype(ref)
    if finding_type in _RTP_TYPES:
        order = {"RTP_TIMELINE_PNG": 0, "WAVEFORM_PNG": 1, "SPECTROGRAM_PNG": 2, "SPECTRUM_PNG": 3}
    elif finding_type in _SIP_TYPES:
        order = {"SIP_CALL_FLOW_PNG": 0, "RTP_TIMELINE_PNG": 1}
    elif "PERIODIC" in finding_type:
        order = {"SPECTRUM_PNG": 0, "SPECTROGRAM_PNG": 1, "WAVEFORM_PNG": 2, "RTP_TIMELINE_PNG": 3}
    elif finding_type in {"CLICK_POP", "UNEXPECTED_SILENCE"}:
        order = {"WAVEFORM_PNG": 0, "SPECTROGRAM_PNG": 1, "SPECTRUM_PNG": 2}
    else:
        order = {"WAVEFORM_PNG": 0, "SPECTROGRAM_PNG": 1, "SPECTRUM_PNG": 2, "RTP_TIMELINE_PNG": 3, "SIP_CALL_FLOW_PNG": 4}
    return order.get(atype, 99), str(ref.get("artifact_id") or "")


def _audio_role(finding_type: str, ref: dict) -> str:
    meta = _metadata(ref)
    if _atype(ref) == "PERIODIC_AUDIO_CLIP":
        source = str(meta.get("source") or "").lower()
        if source == "pcm_rx":
            return "PRIMARY_AUDIO"
        if source == "rtp_up":
            return "COMPARISON_AUDIO"
        if source == "rtp_down":
            return "CONTROL_AUDIO"
        return "COMPARISON_AUDIO"
    if _atype(ref) == "AUDIO_CLIP":
        return "PRIMARY_AUDIO"
    if _atype(ref) in _FULL_AUDIO_TYPES:
        return "SOURCE_AUDIO"
    return "OTHER"


def _packet_refs(finding: dict) -> list[dict]:
    refs: list[dict] = []
    metrics = finding.get("metrics") or {}
    incidents = metrics.get("incidents") or []
    for incident in incidents:
        for ref in incident.get("packet_refs") or []:
            refs.append({
                "incident_id": incident.get("incident_id"),
                "stream_id": incident.get("stream_id"),
                "call_relative_time_seconds": incident.get("call_relative_time_seconds"),
                **dict(ref),
            })
    if not refs:
        for ref in finding.get("event_refs") or []:
            if ref.get("source") == "pcap.frame" or ref.get("frame_number") is not None:
                refs.append(dict(ref))
    seen = set()
    out = []
    for ref in refs:
        key = (ref.get("frame_number"), ref.get("role"), ref.get("incident_id"))
        if key in seen:
            continue
        seen.add(key)
        out.append(ref)
    return out


def _stream_refs(finding: dict) -> list[dict]:
    scope = finding.get("scope") or {}
    out = []
    for role, key in (
        ("RTP_STREAM", "rtp_stream_id"),
        ("RTP_UPSTREAM", "upstream_rtp_stream_id"),
        ("RTP_DOWNSTREAM", "downstream_rtp_stream_id"),
    ):
        if scope.get(key):
            out.append({"role": role, "stream_id": scope.get(key)})
    if scope.get("pcm_tap"):
        out.append({
            "role": "PCM_TAP",
            "pcm_tap": scope.get("pcm_tap"),
            "pcm_session_index": scope.get("pcm_session_index"),
        })
    return out


def _key_metrics(finding: dict) -> dict:
    metrics = finding.get("metrics") or {}
    ftype = str(finding.get("type") or "")
    if ftype == "HIGH_DELTA":
        incidents = metrics.get("incidents") or []
        return {
            "incident_count": metrics.get("incident_count", len(incidents)),
            "delta_ms": [x.get("delta_ms") for x in incidents if x.get("delta_ms") is not None],
            "expected_ptime_ms": [x.get("expected_ptime_ms") for x in incidents if x.get("expected_ptime_ms") is not None],
            "packet_loss_relation": metrics.get("packet_loss_relation"),
            "stream_lost_packets": [x.get("stream_lost_packets") for x in incidents if x.get("stream_lost_packets") is not None],
        }
    if ftype in {"PACKET_LOSS", "BURST_LOSS"}:
        return {k: metrics.get(k) for k in ("lost_packets", "loss_rate", "packet_count", "ptime_ms", "estimated_audio_loss_ms") if metrics.get(k) is not None}
    if "PERIODIC" in ftype:
        return {k: metrics.get(k) for k in ("hum", "signal", "strength", "pcm_rx", "upstream_rtp", "downstream_rtp") if metrics.get(k) is not None}
    if ftype in {"CLICK_POP", "UNEXPECTED_SILENCE"}:
        return {k: metrics.get(k) for k in ("duration_ms", "jump", "confidence", "threshold_dbfs", "candidate_id", "candidate_reason_codes", "counterpart_rtp_stream_id", "pcm_rtp_absolute_correlation") if metrics.get(k) is not None}
    return dict(list(metrics.items())[:16])


def _requirements(finding: dict) -> dict:
    ftype = str(finding.get("type") or "")
    severity = str(finding.get("severity") or "INFO").upper()
    needs_graph = severity in {"MEDIUM", "HIGH", "CRITICAL"}
    return {
        "primary_graph": needs_graph,
        "primary_audio_clip": ftype in _AUDIBLE_TYPES and severity in {"MEDIUM", "HIGH", "CRITICAL"},
        "packet_refs": ftype in _RTP_TYPES,
        "cross_layer_boundary": ftype in _CROSS_LAYER_REQUIRED_TYPES,
    }


def _next_validation(finding_type: str) -> str:
    if finding_type == "LOCAL_CAPTURE_PERIODIC_INTERFERENCE":
        return "保持其他条件不变，依次执行电源 A/B、话机/线材 A/B、FXS 端口/设备 A/B，确认周期干扰在哪一步消失或迁移。"
    if finding_type == "PERIODIC_LOW_FREQUENCY_INTERFERENCE":
        return "补齐同时间窗的上游/下游媒体对照；在跨层证据不足时不要确认物理噪声来源。"
    if finding_type == "HIGH_DELTA":
        return "对照同时间窗 PCM RX/TX、RTP 反向流和发送端调度/抓包节奏，区分设备发送停顿、捕获停顿与网络时延。"
    if finding_type in {"PACKET_LOSS", "BURST_LOSS"}:
        return "沿 RTP 路径补充两端抓包或接口统计，确认 Sequence 缺口首次出现的位置，并检查是否存在链路拥塞/丢弃。"
    if finding_type == "UNEXPECTED_SILENCE":
        return "复核同时间窗的 correlated RTP input 与 PCM Tap；只有 RTP 有有效音频而 PCM 变静音时，才继续定位播放/处理链路。"
    if finding_type == "CLICK_POP":
        return "先试听异常 Clip，并继续排除 DTMF、Hook、拨号音/回铃音等正常瞬态；必要时做话机/线路/端口 A/B。"
    if finding_type == "ECHO_PATH_DETECTED":
        return "结合用户实际听感、TX/RX 延迟和相关性做同步试听；必要时调整回声路径或 AEC 配置后复测。"
    if finding_type in _SIP_TYPES:
        return "沿 SIP Call Flow 下钻到关键 Frame、状态码和 SDP，确认失败/协商异常发生的具体事务与方向。"
    return "复核对应时间窗、原始 Evidence 和对照层，再决定是否进入 A/B 或 Fix Verification。"


def build_finding_evidence_package(finding: dict, artifact_refs: list[dict]) -> dict:
    ftype = str(finding.get("type") or "")
    graphs = sorted([dict(x) for x in artifact_refs if _atype(x) in _GRAPH_TYPES], key=lambda x: _graph_priority(ftype, x))
    audio = [dict(x) for x in artifact_refs if _atype(x) in _AUDIO_CLIP_TYPES | _FULL_AUDIO_TYPES]
    primary_audio = next((x for x in audio if _audio_role(ftype, x) == "PRIMARY_AUDIO"), None)
    comparison_audio = [x for x in audio if _audio_role(ftype, x) == "COMPARISON_AUDIO"]
    control_audio = [x for x in audio if _audio_role(ftype, x) == "CONTROL_AUDIO"]
    source_audio = [x for x in audio if _audio_role(ftype, x) == "SOURCE_AUDIO"]
    correlation = finding.get("correlation") or {}
    cross = correlation.get("cross_layer_observation")
    boundary = correlation.get("first_observable_boundary") or (cross or {}).get("first_observable_boundary")
    packet_refs = _packet_refs(finding)
    requirements = _requirements(finding)
    missing = []
    if requirements["primary_graph"] and not graphs:
        missing.append("PRIMARY_GRAPH")
    if requirements["primary_audio_clip"] and not primary_audio:
        missing.append("PRIMARY_AUDIO_CLIP")
    if requirements["packet_refs"] and not packet_refs:
        missing.append("PACKET_REFS")
    if requirements["cross_layer_boundary"] and not boundary:
        missing.append("CROSS_LAYER_BOUNDARY")
    if not missing:
        reviewability = "FULLY_REVIEWABLE"
    elif len(missing) >= sum(1 for v in requirements.values() if v):
        reviewability = "NOT_REVIEWABLE"
    else:
        reviewability = "PARTIALLY_REVIEWABLE"
    return {
        "schema_version": FINDING_EVIDENCE_PACKAGE_VERSION,
        "finding_id": finding.get("finding_id"),
        "finding_type": ftype,
        "reviewability": reviewability,
        "requirements": requirements,
        "missing_required_evidence": missing,
        "primary_graph": graphs[0] if graphs else None,
        "supporting_graphs": graphs[1:],
        "primary_audio_clip": primary_audio,
        "comparison_audio_clips": comparison_audio,
        "control_audio_clips": control_audio,
        "source_audio": source_audio,
        "packet_refs": packet_refs,
        "stream_refs": _stream_refs(finding),
        "key_metrics": _key_metrics(finding),
        "time_range": dict(finding.get("time_range") or {}),
        "cross_layer_observation": cross,
        "first_observable_boundary": boundary,
        "root_cause_boundary": finding.get("root_cause_boundary"),
        "can_confirm": finding.get("interpretation"),
        "cannot_confirm": finding.get("root_cause_boundary"),
        "next_validation": _next_validation(ftype),
        "artifact_count": len(artifact_refs),
    }


def build_report_evidence_packages(findings: list[dict]) -> dict:
    packages = {}
    counts = {"FULLY_REVIEWABLE": 0, "PARTIALLY_REVIEWABLE": 0, "NOT_REVIEWABLE": 0}
    for finding in findings:
        package = build_finding_evidence_package(finding, finding.get("artifact_refs") or [])
        finding["evidence_package"] = package
        key = str(finding.get("finding_id") or finding.get("stable_key"))
        packages[key] = package
        counts[package["reviewability"]] = counts.get(package["reviewability"], 0) + 1
    return {
        "schema_version": FINDING_EVIDENCE_PACKAGE_VERSION,
        "packages": packages,
        "summary": {"finding_count": len(findings), **counts},
    }
