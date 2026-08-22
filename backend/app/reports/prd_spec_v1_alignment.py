from __future__ import annotations

from collections import defaultdict
from typing import Any

from app.contracts.evidence_report import REPORT_COMPOSER_VERSION, REPORT_SCHEMA_VERSION


ALIGNMENT_CONTRACT_VERSION = "preliminary-evidence-prd-spec-v1-alignment"
COMPLETENESS_DIMENSIONS = ("PCAP", "SIP", "RTP", "PCM_RX", "PCM_TX", "DEBUG", "CORRELATION")
ARTIFACT_PROVENANCE_FIELDS = (
    "artifact_id", "type", "case_id", "session_id", "call_id", "finding_ids",
    "source_artifact_ids", "analyzer_name", "analyzer_version", "profile_version",
    "time_range", "sha256", "size", "mime_type", "storage_location", "created_at",
)
PROVENANCE_NON_NULL_FIELDS = (
    "artifact_id", "type", "case_id", "sha256", "size", "mime_type", "storage_location", "created_at",
)
_INTERRUPTED_CALL_STATES = {"ABORTED", "INCOMPLETE", "CANCELLED"}


def _bool(value: Any) -> bool:
    return bool(value)


def build_evidence_completeness(payload: dict) -> dict:
    """Project legacy completeness into the frozen seven-dimension FR-014 contract.

    This is deliberately fail-closed: a dimension is AVAILABLE only when the
    canonical report already contains affirmative evidence for it. Missing facts
    are never inferred as normal.
    """
    legacy = payload.get("completeness") or {}
    capture = legacy.get("capture") or {}
    packet = payload.get("packet_summary") or {}
    pcm = payload.get("pcm_summary") or {}
    analyzers = legacy.get("analyzers") or {}
    findings = payload.get("findings") or []

    packet_available = _bool(packet.get("available"))
    calls = packet.get("calls") or []
    streams = packet.get("streams") or []
    pcm_available = _bool(pcm.get("available"))
    pcm_streams = pcm.get("streams") or []
    taps = {str((x.get("tap") or {}).get("name") or "").lower() for x in pcm_streams}
    correlation_available = _bool((analyzers.get("media") or {}).get("available")) or any(
        str((f.get("scope") or {}).get("layer") or "").upper() == "CROSS_LAYER"
        or _bool(f.get("correlation")) for f in findings
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
    dimensions = {
        key: {
            "status": "AVAILABLE" if values[key] else "MISSING_OR_UNAVAILABLE",
            "available": values[key],
        }
        for key in COMPLETENESS_DIMENSIONS
    }
    missing = [key for key in COMPLETENESS_DIMENSIONS if not values[key]]
    state = "COMPLETE" if not missing else "PARTIAL"
    return {
        "contract_version": ALIGNMENT_CONTRACT_VERSION,
        "state": state,
        "dimensions": dimensions,
        "missing": missing,
        "boundary": (
            "PCAP/SIP/RTP/PCM RX/PCM TX/Debug/Correlation 七类证据均可用。"
            if not missing else
            "以下证据缺失或不可用：" + ", ".join(missing) + "；缺失方向不得用于排除或根因确认。"
        ),
    }


def build_call_completion_quality(report: Any, payload: dict) -> dict:
    """FR-029 fail-closed boundary for ABORTED/INCOMPLETE Calls.

    Available evidence remains valid for the observed time range, but an interrupted
    Call must never be narrated as if the whole Call lifecycle had been observed.
    """
    scope_type = str(getattr(report, "scope_type", None) or payload.get("scope_type") or (payload.get("scope") or {}).get("type") or "").upper()
    call = payload.get("call") or payload.get("display_call") or {}
    source_status = str(call.get("status") or "UNKNOWN").upper()
    explicit_incomplete = bool(call.get("incomplete"))
    interrupted = scope_type == "CALL" and (explicit_incomplete or source_status in _INTERRUPTED_CALL_STATES)
    return {
        "contract_version": ALIGNMENT_CONTRACT_VERSION,
        "status": "INCOMPLETE" if interrupted else "COMPLETE_OR_NOT_APPLICABLE",
        "source_call_status": source_status,
        "ended_at": call.get("ended_at"),
        "boundary_downgraded": interrupted,
        "statement": (
            "该 Call 为 ABORTED/INCOMPLETE/CANCELLED 或结束证据不完整；仅对已观测时间段和已有 Evidence 作结论，"
            "未采集/未完成时段不得被解释为正常，也不得据此断言整个 Call 或提升 Root Cause Authority。"
            if interrupted else
            "当前没有触发 FR-029 中断 Call 降级边界。"
        ),
    }


def _artifact_provenance(item: dict, *, report: Any) -> dict:
    metadata = item.get("metadata") or {}
    source = metadata.get("source") or {}
    analyzer = metadata.get("analyzer") or {}
    time_range = metadata.get("time_range") or metadata.get("time_window") or metadata.get("window") or {}
    source_ids = metadata.get("source_artifact_ids") or []
    source_id = source.get("source_artifact_id")
    if source_id and source_id not in source_ids:
        source_ids = [*source_ids, source_id]
    return {
        "artifact_id": item.get("artifact_id"),
        "type": item.get("type"),
        "case_id": getattr(report, "case_id", None),
        "session_id": getattr(report, "session_id", None),
        "call_id": getattr(report, "call_id", None),
        "finding_ids": metadata.get("finding_ids") or [],
        "source_artifact_ids": source_ids,
        "analyzer_name": metadata.get("analyzer_name") or analyzer.get("name") or source.get("analyzer_name"),
        "analyzer_version": metadata.get("analyzer_version") or analyzer.get("version") or source.get("analyzer_version"),
        "profile_version": metadata.get("profile_version") or source.get("profile_version"),
        "time_range": time_range,
        "sha256": item.get("sha256"),
        "size": item.get("size") if item.get("size") is not None else item.get("size_bytes", metadata.get("size_bytes")),
        "mime_type": item.get("mime_type") or item.get("content_type"),
        "storage_location": item.get("storage_location") or item.get("object_key") or metadata.get("object_key"),
        "created_at": item.get("created_at") or metadata.get("created_at"),
    }


def validate_artifact_provenance(provenance: dict) -> list[str]:
    """Validate frozen field persistence without rejecting legitimate N/A scope values.

    SPEC §13 requires every provenance field to be saved. session_id/call_id,
    source_artifact_ids, analyzer/profile and time_range may legitimately be empty
    for Case/report-derived artifacts, so their presence is the contract. Core
    identity/integrity/storage fields must additionally carry values.
    """
    missing = [name for name in ARTIFACT_PROVENANCE_FIELDS if name not in provenance]
    missing.extend(
        name for name in PROVENANCE_NON_NULL_FIELDS
        if name in provenance and provenance.get(name) in (None, "")
    )
    return sorted(set(missing))


def finalize_report_contract(report: Any, payload: dict) -> dict:
    """Add the frozen SPEC §5 canonical shape while preserving legacy aliases.

    Existing Web/Feishu/Golden consumers still read schema_version/report_version/
    scope/completeness/normal_and_exclusion_evidence. V1 alignment therefore adds
    canonical fields rather than deleting the legacy fields in this release.
    """
    frozen_completeness = build_evidence_completeness(payload)
    call_quality = build_call_completion_quality(report, payload)
    legacy_completeness = payload.setdefault("completeness", {})
    legacy_completeness["frozen_v1"] = frozen_completeness
    if frozen_completeness["state"] != "COMPLETE" or call_quality["boundary_downgraded"]:
        legacy_completeness["state"] = "PARTIAL"
    if call_quality["boundary_downgraded"]:
        prior = str(legacy_completeness.get("boundary") or "").strip()
        legacy_completeness["boundary"] = (call_quality["statement"] + (" " + prior if prior else "")).strip()
        context = payload.setdefault("analysis_context", {})
        context["reviewability"] = "NOT_FULLY_REVIEWABLE"
        issues = list(context.get("semantic_issues") or [])
        if "CALL_LIFECYCLE_INCOMPLETE" not in issues:
            issues.append("CALL_LIFECYCLE_INCOMPLETE")
        context["semantic_issues"] = issues
        boundary = payload.setdefault("evidence_boundary", {})
        statement = str(boundary.get("statement") or "").strip()
        boundary["statement"] = (call_quality["statement"] + (" " + statement if statement else "")).strip()

    final_status = "COMPLETE" if legacy_completeness.get("state") == "COMPLETE" else "PARTIAL_COMPLETE"
    if hasattr(report, "status"):
        report.status = final_status

    payload["schema"] = REPORT_SCHEMA_VERSION
    payload["report_id"] = getattr(report, "id", None)
    payload["scope_type"] = getattr(report, "scope_type", None) or (payload.get("scope") or {}).get("type")
    payload["scope_id"] = getattr(report, "scope_id", None) or (payload.get("scope") or {}).get("id")
    payload["version"] = getattr(report, "version", None) or payload.get("report_version")
    payload["status"] = final_status
    payload["capture_quality"] = frozen_completeness
    payload["call_completion_quality"] = call_quality
    payload["signaling_summary"] = {
        "available": _bool((payload.get("packet_summary") or {}).get("available")),
        "sip_message_count": (payload.get("packet_summary") or {}).get("sip_message_count"),
        "calls": (payload.get("packet_summary") or {}).get("calls") or [],
    }
    payload["media_flows"] = (payload.get("packet_summary") or {}).get("streams") or []
    payload["normal_evidence"] = payload.get("normal_and_exclusion_evidence") or []

    provenance = []
    missing_by_artifact: dict[str, list[str]] = {}
    for item in payload.get("artifacts") or []:
        row = _artifact_provenance(item, report=report)
        provenance.append(row)
        missing = validate_artifact_provenance(row)
        if missing:
            missing_by_artifact[str(row.get("artifact_id") or "UNKNOWN")] = missing
    payload["artifact_provenance"] = provenance
    payload["artifact_provenance_status"] = {
        "contract_version": ALIGNMENT_CONTRACT_VERSION,
        "complete": not missing_by_artifact,
        "missing_by_artifact": missing_by_artifact,
    }
    payload["traceability"] = {
        "contract_version": ALIGNMENT_CONTRACT_VERSION,
        "input_snapshot_hash": payload.get("input_snapshot_hash"),
        "schema_version": REPORT_SCHEMA_VERSION,
        "composer_version": payload.get("composer_version") or REPORT_COMPOSER_VERSION,
        "analyzers": payload.get("analyzers") or {},
        "environment_fingerprint": payload.get("environment_fingerprint"),
        "artifact_provenance_complete": not missing_by_artifact,
        "call_completion_boundary": call_quality["status"],
    }
    return payload


def scalar_media_metrics(snapshot: dict) -> dict[str, float]:
    """Extract comparable frozen A/B dimensions from one report snapshot."""
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
