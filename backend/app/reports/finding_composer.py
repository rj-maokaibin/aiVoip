from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from typing import Any

from app.contracts.evidence_report import (
    DEFAULT_ROOT_CAUSE_BOUNDARY,
    EVIDENCE_LEVEL_ORDER,
    FINDING_SIGNATURE_VERSION,
    PERIODIC_ROOT_CAUSE_BOUNDARY,
    SEVERITY_ORDER,
    EvidenceFindingStatus,
)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _norm_severity(value: str | None) -> str:
    raw = str(value or "INFO").upper()
    if raw == "LOW":
        return "INFO"
    return raw if raw in SEVERITY_ORDER else "INFO"


def _time_range(event: dict) -> dict:
    start = event.get("start_time")
    if start is None:
        start = event.get("time")
    end = event.get("end_time", start)
    representative = event.get("representative_time", start)
    return {"start": start, "end": end, "representative": representative}


def finding_signature(*, finding_type: str, layer: str | None, direction: str | None,
                      feature_family: str | None, path_role: str | None) -> str:
    values = [finding_type, layer or "unknown", direction or "unknown",
              feature_family or "generic", path_role or "generic", FINDING_SIGNATURE_VERSION]
    return "|".join(str(x).lower() for x in values)


def stable_key(signature: str) -> str:
    return hashlib.sha256(signature.encode("utf-8")).hexdigest()[:24]


def _base_finding(*, finding_type: str, severity: str, evidence_level: str, title: str,
                  observation: str, scope: dict | None, metrics: dict | None,
                  time_range: dict, source_run_id: str | None,
                  feature_family: str | None = None,
                  interpretation: str | None = None,
                  root_cause_boundary: str | None = None,
                  correlation: dict | None = None,
                  evidence_refs: list | None = None,
                  artifact_refs: list | None = None,
                  event_refs: list | None = None) -> dict:
    scope = scope or {}
    layer = scope.get("layer") or scope.get("pcm_tap") or scope.get("source")
    direction = scope.get("direction") or scope.get("rtp_direction")
    path_role = scope.get("path_role") or scope.get("stream_role")
    sig = finding_signature(
        finding_type=finding_type, layer=layer, direction=direction,
        feature_family=feature_family, path_role=path_role,
    )
    return {
        "stable_key": stable_key(sig),
        "finding_signature": sig,
        "signature_version": FINDING_SIGNATURE_VERSION,
        "type": finding_type,
        "status": EvidenceFindingStatus.OBSERVED.value,
        "severity": _norm_severity(severity),
        "evidence_level": str(evidence_level or "L3").upper(),
        "title": title,
        "observation": observation,
        "interpretation": interpretation or "这是当前 Case 的可复核证据观察，不等同于最终根因。",
        "root_cause_boundary": root_cause_boundary or DEFAULT_ROOT_CAUSE_BOUNDARY,
        "time_range": time_range,
        "scope": scope,
        "metrics": metrics or {},
        "evidence_refs": evidence_refs or [],
        "artifact_refs": artifact_refs or [],
        "event_refs": event_refs or [],
        "correlation": correlation or {},
        "source_analyzer_run_ids": [source_run_id] if source_run_id else [],
        "occurrence_count": 1,
    }


PACKET_TITLE = {
    "SIP_REGISTRATION_FAILED": "SIP 注册失败",
    "SIP_CALL_FAILED": "SIP 呼叫建立失败",
    "SIP_CONFLICTING_FINAL_RESPONSE": "SIP 最终响应存在冲突",
    "ONE_WAY_RTP_MEDIA": "RTP 单向媒体",
    "CODEC_NEGOTIATION_MISMATCH": "编解码协商与实际 RTP 不一致",
    "PACKET_LOSS": "RTP 丢包",
    "BURST_LOSS": "RTP 突发丢包",
    "HIGH_DELTA": "RTP 包间隔异常增大",
    "PAYLOAD_CHANGE": "RTP Payload Type 发生变化",
}


def _packet_findings(packet: dict | None, source_run_id: str | None) -> list[dict]:
    if not packet:
        return []
    out: list[dict] = []
    streams = {s.get("stream_id"): s for s in packet.get("rtp_streams", [])}
    for index, anomaly in enumerate(packet.get("anomalies", []) or []):
        ftype = str(anomaly.get("type") or "PACKET_ANOMALY")
        evidence = anomaly.get("evidence") or {}
        stream = streams.get(evidence.get("stream_id")) or {}
        scope = {
            "layer": "RTP" if ftype in {"PACKET_LOSS", "BURST_LOSS", "HIGH_DELTA", "PAYLOAD_CHANGE", "ONE_WAY_RTP_MEDIA"} else "SIP_SDP",
            "call_id": evidence.get("call_id"),
            "rtp_stream_id": evidence.get("stream_id"),
            "direction": (
                f"{stream.get('src_ip')}:{stream.get('src_port')}->{stream.get('dst_ip')}:{stream.get('dst_port')}"
                if stream else None
            ),
        }
        metrics = dict(evidence)
        if stream:
            metrics.update({
                "packet_count": stream.get("packet_count"),
                "lost_packets": stream.get("lost_packets", stream.get("lost")),
                "loss_rate": stream.get("loss_rate"),
                "p95_jitter_ms": stream.get("p95_rfc3550_jitter_ms"),
                "max_delta_ms": stream.get("max_delta_ms"),
                "codec": stream.get("codec"),
                "ptime_ms": stream.get("ptime_ms"),
            })
        out.append(_base_finding(
            finding_type=ftype,
            severity=anomaly.get("severity", "INFO"),
            evidence_level="L2",
            title=PACKET_TITLE.get(ftype, f"网络媒体异常：{ftype}"),
            observation=f"Packet Analyzer 在当前抓包中观测到 {ftype}。",
            scope=scope,
            metrics=metrics,
            time_range=_time_range(anomaly),
            source_run_id=source_run_id,
            feature_family=ftype,
            evidence_refs=[{"type": "ANALYZER_RUN", "id": source_run_id}] if source_run_id else [],
            event_refs=[{"source": "packet.anomalies", "index": index}],
        ))
    return out


def _pcm_findings(pcm: dict | None, source_run_id: str | None) -> list[dict]:
    if not pcm:
        return []
    out: list[dict] = []
    for stream in pcm.get("streams", []) or []:
        tap = stream.get("tap") or {}
        tap_name = str(tap.get("name") or "pcm")
        direction = str(tap.get("direction") or "").upper() or None
        for session in stream.get("sessions", []) or []:
            base_scope = {
                "layer": tap_name.upper(),
                "pcm_tap": tap_name,
                "pcm_direction": direction,
                "pcm_session_index": session.get("session_index"),
                "direction": direction,
            }
            for index, gap in enumerate(session.get("gap_events", []) or []):
                event = {"time": gap.get("time")}
                out.append(_base_finding(
                    finding_type="PCM_GAP", severity="MEDIUM", evidence_level="L2",
                    title=f"{tap_name} 数据间隙",
                    observation=f"{tap_name} 检测到 PCM 数据包间隔异常。",
                    scope=base_scope, metrics=gap, time_range=_time_range(event),
                    source_run_id=source_run_id, feature_family="packet_gap",
                    evidence_refs=[{"type": "ANALYZER_RUN", "id": source_run_id}] if source_run_id else [],
                    event_refs=[{"source": "pcm.gap_events", "index": index}],
                ))
            hum = session.get("hum") or {}
            if str(hum.get("level") or "LOW").upper() in {"MEDIUM", "HIGH"}:
                severity = "HIGH" if str(hum.get("level")).upper() == "HIGH" else "MEDIUM"
                out.append(_base_finding(
                    finding_type="PERIODIC_LOW_FREQUENCY_INTERFERENCE", severity=severity,
                    evidence_level="L2", title=f"{tap_name} 检测到周期性低频/工频族特征",
                    observation=(f"{tap_name} 的 {hum.get('dominant_family', '50/60Hz')} 频率族得分为 "
                                 f"{hum.get('score')}，达到 {hum.get('level')} 等级。"),
                    scope=base_scope,
                    metrics={"hum": hum, "signal": session.get("signal") or {}},
                    time_range={"start": session.get("start_time"), "end": session.get("end_time"), "representative": session.get("start_time")},
                    source_run_id=source_run_id,
                    feature_family=str(hum.get("dominant_family") or "mains_family"),
                    root_cause_boundary=PERIODIC_ROOT_CAUSE_BOUNDARY,
                    evidence_refs=[{"type": "ANALYZER_RUN", "id": source_run_id}] if source_run_id else [],
                ))
            for index, ev in enumerate(session.get("silence_events", []) or []):
                start = (session.get("start_time") or 0) + float(ev.get("start_seconds", 0))
                end = (session.get("start_time") or 0) + float(ev.get("end_seconds", ev.get("start_seconds", 0)))
                out.append(_base_finding(
                    finding_type="UNEXPECTED_SILENCE", severity="MEDIUM", evidence_level="L3",
                    title=f"{tap_name} 异常静音候选", observation=f"{tap_name} 检测到活跃音频中的持续静音候选。",
                    scope=base_scope, metrics=ev,
                    time_range={"start": start, "end": end, "representative": start},
                    source_run_id=source_run_id, feature_family="silence",
                    event_refs=[{"source": "pcm.silence_events", "index": index}],
                ))
            for index, ev in enumerate(session.get("click_pop_events", []) or []):
                when = (session.get("start_time") or 0) + float(ev.get("time_seconds", 0))
                out.append(_base_finding(
                    finding_type="CLICK_POP", severity="MEDIUM", evidence_level="L3",
                    title=f"{tap_name} Click/Pop（点击声/爆音）候选",
                    observation=f"{tap_name} 检测到符合多特征门限的瞬态 Click/Pop 候选。",
                    scope=base_scope, metrics=ev,
                    time_range={"start": when, "end": when, "representative": when},
                    source_run_id=source_run_id, feature_family="click_pop",
                    event_refs=[{"source": "pcm.click_pop_events", "index": index}],
                ))
    return out


def _media_findings(media: dict | None, source_run_id: str | None) -> list[dict]:
    if not media:
        return []
    out: list[dict] = []
    events = []
    events.extend(media.get("cross_layer_events", []) or [])
    events.extend(media.get("periodic_interference_paths", []) or [])
    seen: set[str] = set()
    for index, event in enumerate(events):
        fingerprint = _canonical({"type": event.get("type"), "time": event.get("time"), "scope": event.get("scope")})
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        ftype = str(event.get("type") or "MEDIA_ANOMALY")
        if ftype == "PERIODIC_INTERFERENCE_PATH_COMPARISON" and str((event.get("details") or {}).get("level")) == "LOW":
            continue
        scope = dict(event.get("scope") or {})
        scope.setdefault("layer", scope.get("pcm_tap") or "CROSS_LAYER")
        details = event.get("details") or {}
        boundary = details.get("evidence_boundary") or DEFAULT_ROOT_CAUSE_BOUNDARY
        title_map = {
            "LOCAL_CAPTURE_PERIODIC_INTERFERENCE": "本地采集链路周期性干扰",
            "PERIODIC_INTERFERENCE_PATH_COMPARISON": "跨层周期干扰对比",
            "UNEXPECTED_SILENCE": "活跃媒体窗口异常静音",
            "CLICK_POP": "活跃媒体窗口 Click/Pop（点击声/爆音）",
            "ECHO_PATH_DETECTED": "检测到回声路径特征",
        }
        out.append(_base_finding(
            finding_type=ftype,
            severity=event.get("severity", "INFO"),
            evidence_level=event.get("evidence_level", "L2" if ftype == "LOCAL_CAPTURE_PERIODIC_INTERFERENCE" else "L3"),
            title=title_map.get(ftype, f"跨层媒体观察：{ftype}"),
            observation=details.get("interpretation") or f"Media Analyzer 在同一媒体时间窗口观测到 {ftype}。",
            interpretation=details.get("interpretation"),
            root_cause_boundary=boundary,
            scope=scope,
            metrics=details,
            time_range=_time_range(event),
            source_run_id=source_run_id,
            feature_family=("periodic" if "PERIODIC" in ftype else ftype),
            correlation=details.get("correlation") or {},
            evidence_refs=[{"type": "ANALYZER_RUN", "id": source_run_id}] if source_run_id else [],
            event_refs=[{"source": "media.cross_layer", "index": index}],
        ))
    return out


def derive_first_observable_layer(layer_observations: list[dict]) -> dict:
    """Return a deterministic Evidence Boundary, never a physical root cause.

    Each item must be ordered along the media path and carry
    ``available`` plus ``abnormal``.  A layer can only be declared first
    observable when every earlier comparable layer is available and normal.
    """
    if not layer_observations:
        return {"status": "UNKNOWN", "reason": "NO_LAYER_OBSERVATIONS"}
    for index, item in enumerate(layer_observations):
        if not item.get("available"):
            return {"status": "UNKNOWN", "reason": "UPSTREAM_EVIDENCE_MISSING", "missing_layer": item.get("layer")}
        if item.get("abnormal"):
            previous = layer_observations[:index]
            if any(not x.get("available") for x in previous):
                return {"status": "UNKNOWN", "reason": "UPSTREAM_EVIDENCE_MISSING"}
            if any(x.get("abnormal") for x in previous):
                continue
            return {
                "status": "OBSERVED_BOUNDARY",
                "first_observable_layer": item.get("layer"),
                "statement": f"异常首次可观测于 {item.get('layer')}；这是证据边界，不等于异常物理起源或最终根因。",
            }
    return {"status": "NO_COMPARABLE_ANOMALY", "reason": "ALL_AVAILABLE_LAYERS_NORMAL"}


def _merge_same_signature(findings: list[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for finding in findings:
        grouped[finding["finding_signature"]].append(finding)
    out: list[dict] = []
    for signature, items in grouped.items():
        items.sort(key=lambda x: (x["time_range"].get("start") is None, x["time_range"].get("start") or 0))
        head = dict(items[0])
        head["occurrence_count"] = len(items)
        head["source_analyzer_run_ids"] = sorted({rid for item in items for rid in item.get("source_analyzer_run_ids", [])})
        head["evidence_refs"] = [dict(x) for item in items for x in item.get("evidence_refs", [])]
        head["event_refs"] = [dict(x) for item in items for x in item.get("event_refs", [])]
        starts = [x["time_range"].get("start") for x in items if x["time_range"].get("start") is not None]
        ends = [x["time_range"].get("end") for x in items if x["time_range"].get("end") is not None]
        if starts:
            head["time_range"] = {"start": min(starts), "end": max(ends or starts), "representative": starts[0]}
        if len(items) > 1:
            head["observation"] = f"{head['observation']} 同类事件共 {len(items)} 次。"
        out.append(head)
    return out


def sort_findings(findings: list[dict]) -> list[dict]:
    return sorted(
        findings,
        key=lambda f: (
            -SEVERITY_ORDER.get(f.get("severity", "INFO"), 0),
            -int(f.get("symptom_relevance", 0)),
            -EVIDENCE_LEVEL_ORDER.get(f.get("evidence_level", "L5"), 0),
            -int(bool((f.get("correlation") or {}).get("quality") in {"HIGH", "STRONG"})),
            f.get("time_range", {}).get("representative") if f.get("time_range", {}).get("representative") is not None else float("inf"),
            f.get("finding_signature", ""),
        ),
    )


def compose_findings(*, packet: dict | None = None, pcm: dict | None = None,
                     media: dict | None = None, source_run_ids: dict[str, str] | None = None) -> list[dict]:
    source_run_ids = source_run_ids or {}
    findings = []
    findings.extend(_packet_findings(packet, source_run_ids.get("packet_intelligence")))
    findings.extend(_pcm_findings(pcm, source_run_ids.get("pcm_intelligence")))
    findings.extend(_media_findings(media, source_run_ids.get("media_intelligence")))
    return sort_findings(_merge_same_signature(findings))


def build_normal_evidence(packet: dict | None, pcm: dict | None, media: dict | None) -> list[dict]:
    normal: list[dict] = []
    if packet:
        calls = packet.get("calls", []) or []
        if calls and all(c.get("state") in {"ESTABLISHED", "TERMINATED"} for c in calls):
            normal.append({"type": "SIP_CALL_ESTABLISHED", "text": "SIP 呼叫建立流程完整，未发现建链失败。"})
        streams = packet.get("rtp_streams", []) or []
        if streams and all(float(s.get("loss_rate") or 0) == 0 for s in streams):
            normal.append({"type": "RTP_NO_LOSS", "text": "当前 RTP Stream 未检测到丢包。"})
        if calls and all((c.get("media_direction_health") or {}).get("status") == "BIDIRECTIONAL" for c in calls if (c.get("media_direction_health") or {}).get("eligible")):
            normal.append({"type": "RTP_BIDIRECTIONAL", "text": "已建立 Call 的 RTP 媒体方向为双向。"})
    if pcm:
        if int((pcm.get("summary") or {}).get("total_packets") or 0) > 0:
            normal.append({"type": "PCM_PRESENT", "text": "PCM 诊断数据已采集并可用于分析。"})
    if media and not (media.get("degraded_reason")):
        normal.append({"type": "MEDIA_ANALYSIS_FULL", "text": "Media Analyzer 未发生解析降级。"})
    return normal
