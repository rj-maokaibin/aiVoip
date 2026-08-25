from __future__ import annotations

from collections import defaultdict
from typing import Any

from app.contracts.evidence_report import REPORT_COMPOSER_VERSION, REPORT_SCHEMA_VERSION
from app.reports.actionable_summary import attach_actionable_summary
from app.reports.evidence_card import attach_evidence_cards


ALIGNMENT_CONTRACT_VERSION = "preliminary-evidence-prd-spec-v1-alignment-v2"
REPORT_PROJECTION_CONTRACT_VERSION = "preliminary-evidence-canonical-projection-v2"
COMPLETENESS_DIMENSIONS = ("PCAP", "SIP", "RTP", "PCM_RX", "PCM_TX", "DEBUG", "CORRELATION")
REQUIRED_DIMENSIONS = {"PCAP", "SIP", "RTP", "PCM_RX", "PCM_TX", "CORRELATION"}
OPTIONAL_DIMENSIONS = {"DEBUG"}
ARTIFACT_PROVENANCE_FIELDS = (
    "artifact_id", "type", "case_id", "session_id", "call_id", "finding_ids",
    "source_artifact_ids", "analyzer_name", "analyzer_version", "profile_version",
    "time_range", "sha256", "size", "mime_type", "storage_location", "created_at",
)
PROVENANCE_NON_NULL_FIELDS = (
    "artifact_id", "type", "case_id", "sha256", "size", "mime_type", "storage_location", "created_at",
)
_INTERRUPTED_CALL_STATES = {"ABORTED", "INCOMPLETE", "CANCELLED"}
_VISUAL_REQUIRED_FINDINGS = {
    "PACKET_LOSS", "BURST_LOSS", "HIGH_DELTA", "PCM_GAP", "UNEXPECTED_SILENCE", "CLICK_POP",
    "PERIODIC_LOW_FREQUENCY_INTERFERENCE", "LOCAL_CAPTURE_PERIODIC_INTERFERENCE", "ECHO_PATH_DETECTED",
}
_AUDIO_EXPECTED_FINDINGS = {
    "PACKET_LOSS", "BURST_LOSS", "HIGH_DELTA", "PCM_GAP", "UNEXPECTED_SILENCE", "CLICK_POP",
    "PERIODIC_LOW_FREQUENCY_INTERFERENCE", "LOCAL_CAPTURE_PERIODIC_INTERFERENCE", "ECHO_PATH_DETECTED", "DTMF_ABNORMAL",
}
D112_ORDER = [
    "0. 当前状态 / 快速导航",
    "1. 当前初步结论",
    "2. 当前重点问题",
    "3. 证据完整度",
    "4. 最新一次复现结果",
    "5. 多次复现汇总",
    "6. A/B 对比",
    "7. 历次 Reproduction Session",
    "8. 正常项 / 排除性证据",
    "9. 完整技术证据",
    "10. Evidence Bundle / 附件",
    "11. 报告版本与审计记录",
]


def _bool(value: Any) -> bool:
    return bool(value)


def _attr(report: Any, name: str, default: Any = None) -> Any:
    return getattr(report, name, default) if report is not None else default


def _packet_available(payload: dict) -> bool:
    return _bool((payload.get("packet_summary") or {}).get("available"))


def build_evidence_completeness(payload: dict) -> dict:
    """Project evidence into the frozen seven-dimension FR-014 contract.

    The contract distinguishes *display completeness* from *required evidence*.
    DEBUG is always shown, but missing optional Debug does not by itself turn a
    fully analyzable PCAP/PCM/Correlation report into PARTIAL_COMPLETE.
    """
    legacy = payload.get("completeness") or {}
    capture = legacy.get("capture") or {}
    packet = payload.get("packet_summary") or {}
    pcm = payload.get("pcm_summary") or {}
    analyzers = legacy.get("analyzers") or {}
    findings = payload.get("findings") or []

    packet_available = _packet_available(payload)
    calls = packet.get("calls") or []
    streams = packet.get("streams") or payload.get("media_flows") or []
    pcm_available = _bool(pcm.get("available"))
    pcm_streams = pcm.get("streams") or []
    taps = {str((x.get("tap") or {}).get("name") or "").lower() for x in pcm_streams}
    media_state = analyzers.get("media") or {}
    correlation_available = _bool(media_state.get("available")) or any(
        str((f.get("scope") or {}).get("layer") or "").upper() in {"CROSS_LAYER", "PCM_RX_TO_RTP_UPSTREAM"}
        or _bool(f.get("correlation"))
        or str(f.get("type") or "").upper() == "LOCAL_CAPTURE_PERIODIC_INTERFERENCE"
        for f in findings
    )

    values = {
        "PCAP": _bool(capture.get("pcap")),
        "SIP": packet_available and (_bool(calls) or int(packet.get("sip_message_count") or 0) > 0),
        "RTP": packet_available and (_bool(streams) or int(packet.get("rtp_stream_count") or 0) > 0),
        "PCM_RX": pcm_available and ("pcm_rx" in taps or _bool(capture.get("pcm_rx"))),
        "PCM_TX": pcm_available and ("pcm_tx" in taps or _bool(capture.get("pcm_tx"))),
        "DEBUG": _bool(capture.get("debug")),
        "CORRELATION": correlation_available,
    }
    impact = {
        "PCAP": "缺失时无法复核 SIP/RTP 原始网络证据。",
        "SIP": "缺失时无法完整绑定呼叫建立、号码及 SDP 上下文。",
        "RTP": "缺失时无法对网络媒体方向、丢包、抖动和 Delta 作结论。",
        "PCM_RX": "缺失时无法判断被测设备本地接收/采集路径证据。",
        "PCM_TX": "缺失时失去关键 PCM 对照方向。",
        "DEBUG": "当前平台未提供 Debug 时仍可基于 PCAP/PCM 做证据报告，但具体硬件/驱动定位能力受限。",
        "CORRELATION": "缺失时不得声明跨层传播关系或首次可观测边界。",
    }
    dimensions = {}
    for key in COMPLETENESS_DIMENSIONS:
        required = key in REQUIRED_DIMENSIONS
        available = values[key]
        status = "AVAILABLE" if available else "MISSING_REQUIRED" if required else "OPTIONAL_NOT_AVAILABLE"
        dimensions[key] = {
            "status": status,
            "available": available,
            "requirement": "REQUIRED" if required else "OPTIONAL",
            "impact": impact[key],
        }

    missing_required = [key for key in COMPLETENESS_DIMENSIONS if key in REQUIRED_DIMENSIONS and not values[key]]
    missing_optional = [key for key in COMPLETENESS_DIMENSIONS if key in OPTIONAL_DIMENSIONS and not values[key]]
    state = "COMPLETE" if not missing_required else "PARTIAL"
    if missing_required:
        boundary = "以下必需证据缺失或不可用：" + ", ".join(missing_required) + "；对应方向不得用于排除或根因确认。"
    elif missing_optional:
        boundary = (
            "P0 必需证据 PCAP/SIP/RTP/PCM RX/PCM TX/Correlation 均可用；"
            + ", ".join(missing_optional)
            + " 为可选证据且当前不可用，具体硬件/驱动下钻能力受限，但不改变已有 Evidence 事实。"
        )
    else:
        boundary = "PCAP/SIP/RTP/PCM RX/PCM TX/Debug/Correlation 七类证据均可用。"
    return {
        "contract_version": ALIGNMENT_CONTRACT_VERSION,
        "state": state,
        "dimensions": dimensions,
        "missing": missing_required + missing_optional,
        "missing_required": missing_required,
        "missing_optional": missing_optional,
        "boundary": boundary,
    }


def build_call_completion_quality(report: Any, payload: dict) -> dict:
    scope_type = str(_attr(report, "scope_type") or payload.get("scope_type") or (payload.get("scope") or {}).get("type") or "").upper()
    call = payload.get("call") or payload.get("display_call") or {}
    source_status = str(call.get("status") or "UNKNOWN").upper()
    explicit_incomplete = bool(call.get("incomplete"))
    interrupted = scope_type == "CALL" and (explicit_incomplete or source_status in _INTERRUPTED_CALL_STATES)
    return {
        "contract_version": ALIGNMENT_CONTRACT_VERSION,
        "status": "INCOMPLETE" if interrupted else "COMPLETE_OR_NOT_APPLICABLE",
        "source_call_status": source_status,
        "ended_at": call.get("ended_at") or call.get("end_time"),
        "boundary_downgraded": interrupted,
        "statement": (
            "该 Call 为 ABORTED/INCOMPLETE/CANCELLED 或结束证据不完整；仅对已观测时间段和已有 Evidence 作结论，"
            "未采集/未完成时段不得被解释为正常，也不得据此断言整个 Call 或提升 Root Cause Authority。"
            if interrupted else "当前没有触发 FR-029 中断 Call 降级边界。"
        ),
    }


def build_call_formation_quality(report: Any, payload: dict) -> dict:
    scope_type = str(_attr(report, "scope_type") or payload.get("scope_type") or (payload.get("scope") or {}).get("type") or "").upper()
    context = payload.get("analysis_context") or {}
    display_call = payload.get("display_call") or payload.get("call")
    summary = payload.get("multi_call_summary") or {}
    runtime_count = context.get("session_runtime_call_count")
    if runtime_count is None:
        runtime_count = summary.get("call_count")
    diagnostic_count = int(context.get("diagnostic_call_count") or 0)
    no_valid_call = scope_type == "SESSION" and runtime_count == 0 and diagnostic_count == 0 and not display_call
    capture = (payload.get("completeness") or {}).get("capture") or {}
    session_events = list(context.get("session_events") or payload.get("session_events") or [])
    has_offhook = any(
        str(event.get("event_type") or "").upper().replace("_", "").replace("-", "") == "OFFHOOK"
        for event in session_events if isinstance(event, dict)
    )
    preserved = []
    if has_offhook:
        preserved.append("OFF_HOOK")
    if capture.get("pcap"):
        preserved.append("PCAP_PREROLL")
    if capture.get("debug"):
        preserved.append("DEBUG")
    if capture.get("pcm_rx"):
        preserved.append("PCM_RX")
    if capture.get("pcm_tx"):
        preserved.append("PCM_TX")
    display_names = {"OFF_HOOK": "Off-hook", "PCAP_PREROLL": "PCAP 预录", "DEBUG": "Debug", "PCM_RX": "PCM RX", "PCM_TX": "PCM TX"}
    preserved_text = "、".join(display_names[x] for x in preserved) if preserved else "当前没有可确认的前置 Evidence"
    return {
        "contract_version": ALIGNMENT_CONTRACT_VERSION,
        "status": "NO_VALID_CALL" if no_valid_call else "VALID_CALL_OR_NOT_APPLICABLE",
        "no_valid_call": no_valid_call,
        "session_runtime_call_count": runtime_count,
        "diagnostic_call_count": diagnostic_count,
        "preserved_pre_call_evidence": preserved,
        "statement": (
            f"该 Reproduction Session 未形成有效 Call；本报告仅汇总实际已采集的前置 Evidence：{preserved_text}。"
            "未出现的 SIP/RTP/Call 生命周期不得解释为正常通话，也不得据此提升 Root Cause Authority。"
            if no_valid_call else "当前没有触发 FR-028 无有效 Call 边界。"
        ),
    }


def _artifact_provenance(item: dict, *, report: Any) -> dict:
    metadata = item.get("metadata") or {}
    source = metadata.get("source") or {}
    analyzer = metadata.get("analyzer") or {}
    time_range = metadata.get("time_range") or metadata.get("time_window") or metadata.get("anomaly_window") or metadata.get("window") or {}
    source_ids = list(metadata.get("source_artifact_ids") or [])
    source_id = source.get("source_artifact_id") if isinstance(source, dict) else None
    if source_id and source_id not in source_ids:
        source_ids.append(source_id)
    return {
        "artifact_id": item.get("artifact_id"),
        "type": item.get("type"),
        "case_id": _attr(report, "case_id"),
        "session_id": _attr(report, "session_id"),
        "call_id": _attr(report, "call_id"),
        "finding_ids": metadata.get("finding_ids") or [],
        "source_artifact_ids": source_ids,
        "analyzer_name": metadata.get("analyzer_name") or analyzer.get("name") or (source.get("analyzer_name") if isinstance(source, dict) else None),
        "analyzer_version": metadata.get("analyzer_version") or analyzer.get("version") or (source.get("analyzer_version") if isinstance(source, dict) else None),
        "profile_version": metadata.get("profile_version") or (source.get("profile_version") if isinstance(source, dict) else None),
        "time_range": time_range,
        "sha256": item.get("sha256"),
        "size": item.get("size") if item.get("size") is not None else item.get("size_bytes", metadata.get("size_bytes")),
        "mime_type": item.get("mime_type") or item.get("content_type"),
        "storage_location": item.get("storage_location") or item.get("object_key") or item.get("local_path") or metadata.get("object_key"),
        "created_at": item.get("created_at") or metadata.get("created_at"),
    }


def validate_artifact_provenance(provenance: dict) -> list[str]:
    missing = [name for name in ARTIFACT_PROVENANCE_FIELDS if name not in provenance]
    missing.extend(name for name in PROVENANCE_NON_NULL_FIELDS if name in provenance and provenance.get(name) in (None, ""))
    return sorted(set(missing))


def _reviewability(payload: dict, *, provenance_complete: bool) -> dict:
    completeness = payload.get("capture_quality") or {}
    context = payload.get("analysis_context") or {}
    blockers: list[str] = []
    limits: list[str] = []
    if completeness.get("missing_required"):
        blockers.append("REQUIRED_EVIDENCE_MISSING")
    if not provenance_complete and payload.get("artifacts"):
        limits.append("ARTIFACT_PROVENANCE_INCOMPLETE")

    context_reviewability = str(context.get("reviewability") or "").upper()
    semantic_issues = {str(x) for x in (context.get("semantic_issues") or [])}
    if context_reviewability in {"NOT_FULLY_REVIEWABLE", "NOT_REVIEWABLE"} or semantic_issues.intersection({
        "CALL_LIFECYCLE_INCOMPLETE", "CALL_BINDING_INCOMPLETE", "REPORT_SEMANTIC_CONTRADICTION"
    }):
        blockers.append("ANALYSIS_CONTEXT_NOT_FULLY_REVIEWABLE")
    elif context_reviewability == "PARTIALLY_REVIEWABLE":
        limits.append("ANALYSIS_CONTEXT_PARTIALLY_REVIEWABLE")

    for finding in payload.get("findings") or []:
        key = str(finding.get("finding_id") or finding.get("stable_key") or finding.get("type") or "UNKNOWN")
        scope = finding.get("scope") or {}
        tr = finding.get("time_range") or {}
        card = finding.get("evidence_card") or {}
        if not any(scope.get(k) not in (None, "") for k in ("layer", "pcm_tap", "rtp_stream_id", "call_id")):
            blockers.append(f"FINDING_SCOPE_UNBOUND:{key}")
        if tr.get("start") is None and tr.get("end") is None:
            blockers.append(f"FINDING_TIME_UNBOUND:{key}")
        if not finding.get("next_action"):
            blockers.append(f"FINDING_ACTION_MISSING:{key}")
        if not finding.get("verification_acceptance"):
            blockers.append(f"FINDING_ACCEPTANCE_MISSING:{key}")
        if not card:
            blockers.append(f"EVIDENCE_CARD_MISSING:{key}")
            continue
        if finding.get("finding_id") and str(finding.get("severity") or "INFO").upper() in {"MEDIUM", "HIGH", "CRITICAL"}:
            ftype = str(finding.get("type") or "")
            if ftype in _VISUAL_REQUIRED_FINDINGS and not card.get("visual_evidence"):
                blockers.append(f"PRIMARY_VISUAL_MISSING:{key}")
            audio = card.get("audio_evidence") or {}
            if ftype in _AUDIO_EXPECTED_FINDINGS and audio.get("status") != "AVAILABLE":
                limits.append(f"ANOMALY_AUDIO_UNAVAILABLE:{key}")

    if blockers:
        state = "PARTIAL"
    elif limits:
        state = "REVIEWABLE_WITH_LIMITS"
    else:
        state = "FULLY_REVIEWABLE"
    return {
        "contract_version": ALIGNMENT_CONTRACT_VERSION,
        "state": state,
        "fully_reviewable": state == "FULLY_REVIEWABLE",
        "blockers": sorted(set(blockers)),
        "limits": sorted(set(limits)),
    }


def finalize_report_contract(report: Any, payload: dict) -> dict:
    """Produce one internally-consistent Canonical Report projection.

    All downstream views (HTML/Web/Feishu/Bundle) should consume this finalized
    payload. The function is idempotent and never upgrades Root Cause Authority.
    """
    attach_actionable_summary(payload, payload.get("diagnosis") or {})

    frozen_completeness = build_evidence_completeness(payload)
    call_quality = build_call_completion_quality(report, payload)
    formation_quality = build_call_formation_quality(report, payload)
    legacy_completeness = payload.setdefault("completeness", {})
    legacy_completeness["frozen_v1"] = frozen_completeness
    legacy_completeness["state"] = frozen_completeness["state"]
    legacy_completeness["boundary"] = frozen_completeness["boundary"]

    if call_quality["boundary_downgraded"]:
        legacy_completeness["state"] = "PARTIAL"
        legacy_completeness["boundary"] = call_quality["statement"] + " " + legacy_completeness["boundary"]
        context = payload.setdefault("analysis_context", {})
        issues = list(context.get("semantic_issues") or [])
        if "CALL_LIFECYCLE_INCOMPLETE" not in issues:
            issues.append("CALL_LIFECYCLE_INCOMPLETE")
        context["semantic_issues"] = issues
        context["semantic_status"] = "INCOMPLETE"
        context["reviewability"] = "NOT_FULLY_REVIEWABLE"
        boundary = payload.setdefault("evidence_boundary", {})
        boundary["statement"] = call_quality["statement"] + " " + str(boundary.get("statement") or "")

    if formation_quality["no_valid_call"]:
        context = payload.setdefault("analysis_context", {})
        context["session_call_status"] = "NO_VALID_CALL"
        context["semantic_status"] = "INCOMPLETE"
        context["reviewability"] = "NOT_FULLY_REVIEWABLE"
        boundary = payload.setdefault("evidence_boundary", {})
        boundary["statement"] = formation_quality["statement"] + " " + str(boundary.get("statement") or "")
        headline = "本次 Reproduction Session 未形成有效 Call；以下内容仅覆盖实际已采集的前置 Evidence。"
        payload["headline"] = headline
        assessment = payload.setdefault("preliminary_assessment", {})
        assessment["summary"] = headline
        assessment["evidence_boundary"] = formation_quality["statement"]
        assessment["recommended_next_action"] = "复核已保留的前置 Evidence，确认 Call 未形成的位置；缺失的 Call/RTP 阶段不得按正常解释。"

    payload["schema"] = REPORT_SCHEMA_VERSION
    payload["report_id"] = _attr(report, "id") or payload.get("report_id")
    payload["scope_type"] = _attr(report, "scope_type") or payload.get("scope_type") or (payload.get("scope") or {}).get("type")
    payload["scope_id"] = _attr(report, "scope_id") or payload.get("scope_id") or (payload.get("scope") or {}).get("id")
    payload["version"] = _attr(report, "version") or payload.get("version") or payload.get("report_version")
    payload["report_version"] = payload["version"]
    payload["capture_quality"] = frozen_completeness
    payload["call_completion_quality"] = call_quality
    payload["call_formation_quality"] = formation_quality
    payload["session_events"] = list((payload.get("analysis_context") or {}).get("session_events") or [])
    payload["signaling_summary"] = {
        "available": _packet_available(payload),
        "sip_message_count": (payload.get("packet_summary") or {}).get("sip_message_count"),
        "calls": (payload.get("packet_summary") or {}).get("calls") or [],
    }
    payload["media_flows"] = (payload.get("packet_summary") or {}).get("streams") or []
    payload["normal_evidence"] = payload.get("normal_and_exclusion_evidence") or payload.get("normal_evidence") or []
    payload["projection_contract"] = {
        "version": REPORT_PROJECTION_CONTRACT_VERSION,
        "ordering_contract": "D112",
        "ordered_sections": list(D112_ORDER),
        "single_canonical_fact_layer": True,
        "legacy_prepend_revision_allowed": False,
    }

    provenance = []
    missing_by_artifact: dict[str, list[str]] = {}
    for item in payload.get("artifacts") or []:
        row = _artifact_provenance(item, report=report)
        provenance.append(row)
        missing = validate_artifact_provenance(row)
        if missing:
            missing_by_artifact[str(row.get("artifact_id") or row.get("storage_location") or "UNKNOWN")] = missing
    payload["artifact_provenance"] = provenance
    payload["artifact_provenance_status"] = {
        "contract_version": ALIGNMENT_CONTRACT_VERSION,
        "complete": not missing_by_artifact,
        "missing_by_artifact": missing_by_artifact,
    }

    # Cards are generated after actionable scope/time and artifact refs have been
    # finalized so every projection sees exactly the same Finding truth.
    attach_evidence_cards(payload)
    reviewability = _reviewability(payload, provenance_complete=not missing_by_artifact)
    legacy_completeness["reviewability"] = reviewability["state"]
    legacy_completeness["reviewability_contract"] = reviewability
    if reviewability["state"] == "PARTIAL":
        legacy_completeness["state"] = "PARTIAL"

    final_status = "COMPLETE" if legacy_completeness.get("state") == "COMPLETE" else "PARTIAL_COMPLETE"
    payload["status"] = final_status
    if report is not None and hasattr(report, "status"):
        report.status = final_status

    payload["traceability"] = {
        "contract_version": ALIGNMENT_CONTRACT_VERSION,
        "input_snapshot_hash": payload.get("input_snapshot_hash"),
        "schema_version": REPORT_SCHEMA_VERSION,
        "composer_version": payload.get("composer_version") or REPORT_COMPOSER_VERSION,
        "analyzers": payload.get("analyzers") or {},
        "environment_fingerprint": payload.get("environment_fingerprint"),
        "artifact_provenance_complete": not missing_by_artifact,
        "call_completion_boundary": call_quality["status"],
        "call_formation_boundary": formation_quality["status"],
        "reviewability": reviewability,
    }
    payload["canonical_finalization"] = {
        "contract_version": ALIGNMENT_CONTRACT_VERSION,
        "projection_version": 2,
        "finalized": True,
        "finding_count": len(payload.get("findings") or []),
        "forbidden_legacy_overlay": True,
    }
    return payload


def scalar_media_metrics(snapshot: dict) -> dict[str, float]:
    packet = snapshot.get("packet_summary") or {}
    pcm = snapshot.get("pcm_summary") or {}
    streams = packet.get("streams") or []
    pcm_sessions = [s for st in (pcm.get("streams") or []) for s in (st.get("sessions") or [])]

    def avg(values: list[float]) -> float | None:
        return round(sum(values) / len(values), 6) if values else None

    loss = [float(x.get("loss_rate")) for x in streams if x.get("loss_rate") is not None]
    jitter = [float(x.get("p95_jitter_ms")) for x in streams if x.get("p95_jitter_ms") is not None]
    delta = [float(x.get("max_delta_ms")) for x in streams if x.get("max_delta_ms") is not None]
    rms = [float(x.get("rms_dbfs")) for x in pcm_sessions if x.get("rms_dbfs") is not None]
    peak = [float(x.get("peak_dbfs")) for x in pcm_sessions if x.get("peak_dbfs") is not None]
    hum = []
    for x in pcm_sessions:
        score = (x.get("hum") or {}).get("score")
        if score is not None:
            hum.append(float(score))
    return {
        "rtp_loss_rate_mean": avg(loss),
        "rtp_p95_jitter_ms_mean": avg(jitter),
        "rtp_max_delta_ms_mean": avg(delta),
        "pcm_rms_dbfs_mean": avg(rms),
        "pcm_peak_dbfs_mean": avg(peak),
        "spectrum_periodic_score_mean": avg(hum),
    }


def average_metric_rows(rows: list[dict]) -> dict[str, float | None]:
    values: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        for key, value in scalar_media_metrics(row).items():
            if value is not None:
                values[key].append(float(value))
    keys = (
        "rtp_loss_rate_mean", "rtp_p95_jitter_ms_mean", "rtp_max_delta_ms_mean",
        "pcm_rms_dbfs_mean", "pcm_peak_dbfs_mean", "spectrum_periodic_score_mean",
    )
    return {key: (round(sum(values[key]) / len(values[key]), 6) if values[key] else None) for key in keys}
