from __future__ import annotations

from collections import defaultdict

from app.analyzers.candidate_gate import CandidateDecision, build_diagnostic_candidates, candidate_summary
from app.analyzers.cross_layer import derive_first_observable_layer, silence_candidate_observation
from .finding_composer_core import *  # noqa: F401,F403
from .finding_composer_core import (
    _base_finding,
    _media_findings,
    _merge_same_signature,
    _packet_findings,
    _pcm_findings,
    build_normal_evidence as _core_build_normal_evidence,
    sort_findings,
)


_CONTEXT_GATED_AUDIO_TYPES = {"CLICK_POP", "UNEXPECTED_SILENCE"}
_RTP_INCIDENT_TYPES = {"HIGH_DELTA", "PACKET_LOSS", "BURST_LOSS", "PAYLOAD_CHANGE"}


def _resolved_candidates(*, pcm: dict | None, media: dict | None) -> list[dict]:
    existing = list((media or {}).get("diagnostic_candidates", []) or [])
    return existing if existing else build_diagnostic_candidates(pcm=pcm, media=media)


def _candidate_findings(candidates: list[dict], source_run_id: str | None) -> list[dict]:
    out: list[dict] = []
    title_map = {
        "CLICK_POP": "活跃媒体窗口 Click/Pop（点击声/爆音）",
        "UNEXPECTED_SILENCE": "活跃媒体窗口异常静音",
    }
    for candidate in candidates:
        if candidate.get("decision") != CandidateDecision.ACCEPT.value:
            continue
        ftype = str(candidate.get("type") or "AUDIO_CANDIDATE")
        if ftype not in _CONTEXT_GATED_AUDIO_TYPES:
            continue
        scope = dict(candidate.get("scope") or {})
        scope.setdefault("layer", scope.get("pcm_tap") or "CROSS_LAYER")
        metrics = dict(candidate.get("metrics") or {})
        metrics.update({
            "candidate_id": candidate.get("candidate_id"),
            "candidate_decision": candidate.get("decision"),
            "candidate_reason_codes": list(candidate.get("reason_codes") or []),
            "candidate_context": dict(candidate.get("context") or {}),
        })
        observation = (
            f"{scope.get('pcm_tap') or 'PCM'} 的 {ftype} Detector Candidate 已通过 Call 级上下文与 Negative Control Gate。"
        )
        correlation = {"candidate_gate": {
            "decision": candidate.get("decision"),
            "reason_codes": list(candidate.get("reason_codes") or []),
            "context": dict(candidate.get("context") or {}),
        }}
        cross = silence_candidate_observation(candidate)
        if cross:
            correlation["cross_layer_observation"] = cross
            correlation["first_observable_boundary"] = cross.get("first_observable_boundary") or {}
            boundary = correlation["first_observable_boundary"]
            if boundary.get("status") == "OBSERVED_BOUNDARY":
                observation += " " + str(boundary.get("statement") or "")
        out.append(_base_finding(
            finding_type=ftype,
            severity=candidate.get("severity") or "MEDIUM",
            evidence_level=candidate.get("evidence_level") or "L3",
            title=title_map.get(ftype, f"音频异常：{ftype}"),
            observation=observation,
            interpretation=(
                "该事件已通过当前 V1 确定性上下文 Gate，可作为初步证据 Finding；"
                "Cross-Layer Boundary 仅表示当前证据链的首次可观测位置，仍不等于物理根因。"
            ),
            scope=scope,
            metrics=metrics,
            time_range=dict(candidate.get("time_range") or {}),
            source_run_id=source_run_id,
            feature_family=ftype,
            correlation=correlation,
            evidence_refs=[{"type": "ANALYZER_RUN", "id": source_run_id}] if source_run_id else [],
            event_refs=[dict(candidate.get("source_event_ref") or {})] if candidate.get("source_event_ref") else [],
        ))
    return out


def _incident_snapshot(finding: dict) -> dict | None:
    metrics = finding.get("metrics") or {}
    code = metrics.get("incident_semantic_code")
    incident_id = metrics.get("incident_id")
    if not code and not incident_id:
        return None
    direction = metrics.get("direction") or {}
    if isinstance(direction, dict):
        direction_text = direction.get("text")
    else:
        direction_text = direction
    return {
        "incident_id": incident_id,
        "semantic_code": code,
        "semantic_text": metrics.get("incident_semantic_text"),
        "call_id": metrics.get("call_id"),
        "call_relative_time_seconds": metrics.get("call_relative_time_seconds"),
        "stream_id": (finding.get("scope") or {}).get("rtp_stream_id"),
        "direction": direction_text,
        "media_role": metrics.get("media_role"),
        "delta_ms": metrics.get("delta_ms"),
        "expected_ptime_ms": metrics.get("expected_ptime_ms"),
        "excess_delay_ms": metrics.get("excess_delay_ms"),
        "sequence_boundary": metrics.get("sequence_boundary") or {},
        "packet_refs": metrics.get("packet_refs") or [],
        "stream_lost_packets": metrics.get("stream_lost_packets"),
        "stream_loss_rate": metrics.get("stream_loss_rate"),
        "stream_packet_count": metrics.get("stream_packet_count"),
        "stream_p95_jitter_ms": metrics.get("stream_p95_jitter_ms"),
    }


def _semantic_packet_findings(packet: dict | None, source_run_id: str | None) -> list[dict]:
    findings = _packet_findings(packet, source_run_id)
    for finding in findings:
        if finding.get("type") not in _RTP_INCIDENT_TYPES:
            continue
        metrics = finding.get("metrics") or {}
        semantic = metrics.get("incident_semantic_code")
        if not semantic:
            continue
        scope = finding.setdefault("scope", {})
        if metrics.get("call_id") and not scope.get("call_id"):
            scope["call_id"] = metrics.get("call_id")
        direction = metrics.get("direction") or {}
        if isinstance(direction, dict) and direction.get("text"):
            scope["direction"] = direction.get("text")
        correlation = dict(finding.get("correlation") or {})
        correlation["rtp_incident"] = {
            "incident_id": metrics.get("incident_id"),
            "semantic_code": semantic,
            "sequence_boundary": metrics.get("sequence_boundary") or {},
            "call_relative_time_seconds": metrics.get("call_relative_time_seconds"),
            "media_role": metrics.get("media_role"),
        }
        finding["correlation"] = correlation
        for ref in metrics.get("packet_refs") or []:
            finding.setdefault("event_refs", []).append({"source": "pcap.frame", **dict(ref)})
        if finding.get("type") == "HIGH_DELTA":
            delta = metrics.get("delta_ms")
            expected = metrics.get("expected_ptime_ms")
            frame_refs = metrics.get("packet_refs") or []
            frames = " → ".join(str(x.get("frame_number")) for x in frame_refs if x.get("frame_number") is not None)
            t_rel = metrics.get("call_relative_time_seconds")
            time_text = f"Call T+{float(t_rel):.3f}s，" if isinstance(t_rel, (int, float)) else ""
            finding["title"] = "RTP 发送/到达节奏短时停顿（High Delta）"
            finding["observation"] = (
                f"{time_text}{scope.get('direction') or '当前 RTP Stream'} 的相邻包间隔为 {delta} ms，"
                f"预期约 {expected} ms" + (f"；边界 Frame {frames}" if frames else "") + "。"
            )
            finding["interpretation"] = str(metrics.get("incident_semantic_text") or finding.get("interpretation") or "")
        elif finding.get("type") in {"PACKET_LOSS", "BURST_LOSS"}:
            lost = metrics.get("lost_packets")
            finding["observation"] = (
                f"{scope.get('direction') or '当前 RTP Stream'} 的 RTP Sequence 边界确认缺失 {lost} 个包；"
                "previous/next Frame 为丢包边界证据，缺失包本身没有可引用 Frame。"
            )
            finding["interpretation"] = str(metrics.get("incident_semantic_text") or finding.get("interpretation") or "")
    return findings


def _merge_findings_preserving_incidents(findings: list[dict]) -> list[dict]:
    incident_groups: dict[str, list[dict]] = defaultdict(list)
    for finding in findings:
        incident = _incident_snapshot(finding)
        if incident:
            incident_groups[str(finding.get("finding_signature"))].append(incident)
    merged = _merge_same_signature(findings)
    for finding in merged:
        incidents = incident_groups.get(str(finding.get("finding_signature"))) or []
        if not incidents:
            continue
        metrics = dict(finding.get("metrics") or {})
        metrics["incidents"] = incidents
        metrics["incident_count"] = len(incidents)
        finding["metrics"] = metrics
        correlation = dict(finding.get("correlation") or {})
        correlation["rtp_incident_summary"] = {
            "incident_count": len(incidents),
            "semantic_codes": sorted({str(x.get("semantic_code")) for x in incidents if x.get("semantic_code")}),
            "stream_ids": sorted({str(x.get("stream_id")) for x in incidents if x.get("stream_id")}),
        }
        finding["correlation"] = correlation
        if finding.get("type") != "HIGH_DELTA":
            continue
        deltas = [float(x["delta_ms"]) for x in incidents if isinstance(x.get("delta_ms"), (int, float))]
        expected = [float(x["expected_ptime_ms"]) for x in incidents if isinstance(x.get("expected_ptime_ms"), (int, float))]
        directions = sorted({str(x.get("direction")) for x in incidents if x.get("direction")})
        all_boundary_contiguous = bool(incidents) and all((x.get("sequence_boundary") or {}).get("sequence_contiguous") is True for x in incidents)
        known_stream_loss = [x.get("stream_lost_packets") for x in incidents if isinstance(x.get("stream_lost_packets"), int)]
        all_stream_loss_zero = bool(known_stream_loss) and all(x == 0 for x in known_stream_loss)
        delta_text = ", ".join(f"{x:.3f}".rstrip("0").rstrip(".") for x in deltas)
        expected_text = f"{expected[0]:.3f}".rstrip("0").rstrip(".") if expected else "未知"
        direction_text = " / ".join(directions) or (finding.get("scope") or {}).get("direction") or "当前 RTP Stream"
        finding["title"] = "RTP 发送/到达节奏短时停顿（High Delta）"
        finding["observation"] = (
            f"{direction_text} 共观测到 {len(incidents)} 次 High Delta；包间隔为 [{delta_text}] ms，"
            f"预期约 {expected_text} ms。每个事件均保留 Call 相对时间、Frame、Sequence 和 Stream 指标供复核。"
        )
        if all_boundary_contiguous and all_stream_loss_zero:
            finding["interpretation"] = (
                "这些 High Delta 事件边界前后 RTP Sequence 均连续，且对应 Stream 的统计丢包数为 0；"
                "证据支持短时发送/到达节奏停顿，不等同于 RTP Packet Loss（丢包）。"
            )
            metrics["packet_loss_relation"] = "NO_SEQUENCE_GAP_AND_STREAM_LOSS_ZERO"
        elif all_boundary_contiguous:
            finding["interpretation"] = (
                "这些 High Delta 事件边界 RTP Sequence 连续，因此事件边界本身没有 Sequence 丢口；"
                "但 Stream 其他位置的丢包状态需按独立 Packet Loss 证据判断，不能由 High Delta 单独排除。"
            )
            metrics["packet_loss_relation"] = "NO_SEQUENCE_GAP_AT_INCIDENT_BOUNDARIES"
        else:
            finding["interpretation"] = (
                "High Delta 表示 RTP 到达/发送节奏显著偏离 ptime；当前部分事件的 Sequence 边界并非全部连续，"
                "需同时查看 Incident 中的 packet_loss_at_boundary 与独立 Packet Loss Finding。"
            )
            metrics["packet_loss_relation"] = "SEQUENCE_GAP_OR_UNCERTAIN"
    return merged


def compose_findings(*, packet: dict | None = None, pcm: dict | None = None,
                     media: dict | None = None, source_run_ids: dict[str, str] | None = None) -> list[dict]:
    """Compose Findings after deterministic Candidate and RTP Incident contracts."""
    source_run_ids = source_run_ids or {}
    findings: list[dict] = []
    findings.extend(_semantic_packet_findings(packet, source_run_ids.get("packet_intelligence")))
    findings.extend(
        f for f in _pcm_findings(pcm, source_run_ids.get("pcm_intelligence"))
        if f.get("type") not in _CONTEXT_GATED_AUDIO_TYPES
    )
    findings.extend(
        f for f in _media_findings(media, source_run_ids.get("media_intelligence"))
        if f.get("type") not in _CONTEXT_GATED_AUDIO_TYPES
    )
    candidates = _resolved_candidates(pcm=pcm, media=media)
    findings.extend(_candidate_findings(candidates, source_run_ids.get("media_intelligence")))
    return sort_findings(_merge_findings_preserving_incidents(findings))


def build_normal_evidence(packet: dict | None, pcm: dict | None, media: dict | None) -> list[dict]:
    normal = _core_build_normal_evidence(packet, pcm, media)
    candidates = _resolved_candidates(pcm=pcm, media=media)
    if packet:
        incident_summary = packet.get("rtp_incident_summary") or {}
        if int(incident_summary.get("cadence_stall_without_sequence_gap_count") or 0) > 0:
            streams = packet.get("rtp_streams") or []
            if streams and all(int(s.get("lost_packets") or 0) == 0 for s in streams):
                normal.append({
                    "type": "HIGH_DELTA_NOT_PACKET_LOSS",
                    "text": "当前 High Delta 事件存在 Sequence 连续证据，且 RTP Stream 统计丢包为 0；High Delta 不应被解释为 RTP 丢包。",
                })
    if not candidates:
        return normal
    summary = candidate_summary(candidates)
    suppressed = [x for x in candidates if x.get("decision") == CandidateDecision.SUPPRESS.value]
    inconclusive = [x for x in candidates if x.get("decision") == CandidateDecision.INCONCLUSIVE.value]
    if suppressed:
        reasons: dict[str, int] = {}
        for candidate in suppressed:
            for code in candidate.get("reason_codes") or []:
                if code == "ACTIVE_MEDIA_SCOPED":
                    continue
                reasons[code] = reasons.get(code, 0) + 1
        normal.append({
            "type": "AUDIO_CANDIDATES_SUPPRESSED",
            "text": f"{len(suppressed)} 个音频异常 Detector Candidate 因正常业务/跨层对照证据被抑制，不升级为 Finding。",
            "candidate_ids": [x.get("candidate_id") for x in suppressed],
            "reason_counts": reasons,
        })
    if inconclusive:
        normal.append({
            "type": "AUDIO_CANDIDATES_INCONCLUSIVE",
            "text": f"{len(inconclusive)} 个音频异常 Candidate 因跨层证据不足保持 INCONCLUSIVE，不升级为 Finding。",
            "candidate_ids": [x.get("candidate_id") for x in inconclusive],
        })
    normal.append({"type": "DIAGNOSTIC_CANDIDATE_SUMMARY", "text": f"音频 Candidate Gate：总计 {summary['total']}，ACCEPT {summary['decisions'].get('ACCEPT',0)}，SUPPRESS {summary['decisions'].get('SUPPRESS',0)}，INCONCLUSIVE {summary['decisions'].get('INCONCLUSIVE',0)}。", "summary": summary})
    return normal
