from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from .correlation import correlation_problem_count
from .recommendation import generate_recommendations
from .semantic_validator import validate_report_semantics


def compose_preliminary_report_v2(
    *,
    report_id: str,
    call_reconstruction: Mapping[str, Any],
    timeline: Mapping[str, Any],
    rtp_streams: list[Mapping[str, Any]],
    events: list[Mapping[str, Any]],
    findings: list[Mapping[str, Any]],
    correlation_clusters: list[Mapping[str, Any]],
    visibility: Mapping[str, Any],
    normal_evidence: list[Mapping[str, Any]] | None = None,
    exclusion_evidence: list[Mapping[str, Any]] | None = None,
    artifacts: list[Mapping[str, Any]] | None = None,
    artifact_failures: list[Mapping[str, Any]] | None = None,
    preliminary_assessment: Mapping[str, Any] | None = None,
    symptom_assessment: Mapping[str, Any] | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Compose canonical V2 data, validate it, then expose a decision summary.

    Facts are supplied by deterministic upstream components. This composer does
    not derive SIP/RTP facts from prose and does not allow validation failures to
    remain user-visible COMPLETE reports.
    """

    finding_list = [dict(item) for item in findings]
    cluster_list = [dict(item) for item in correlation_clusters]
    visibility_dict = dict(visibility)
    recommendations = generate_recommendations(
        findings=finding_list,
        clusters=cluster_list,
        visibility=visibility_dict,
    )
    report: dict[str, Any] = {
        "schema": "preliminary-evidence-report-v2",
        "report_id": report_id,
        "pipeline_status": "COMPLETE",
        "call_reconstruction": dict(call_reconstruction),
        "timeline": dict(timeline),
        "rtp_streams": [dict(item) for item in rtp_streams],
        "visibility": visibility_dict,
        "events": [dict(item) for item in events],
        "findings": finding_list,
        "correlation_clusters": cluster_list,
        "problem_count": correlation_problem_count(finding_list, cluster_list),
        "normal_evidence": [dict(item) for item in normal_evidence or []],
        "exclusion_evidence": [dict(item) for item in exclusion_evidence or []],
        "artifacts": [dict(item) for item in artifacts or []],
        "artifact_failures": [dict(item) for item in artifact_failures or []],
        "preliminary_assessment": dict(preliminary_assessment or {"root_cause_status": "UNCONFIRMED"}),
        "symptom_assessment": dict(symptom_assessment or {}),
        "recommendations": recommendations,
        "claims": {
            "end_to_end_media_complete": visibility_dict.get("end_to_end_media") == "COMPLETE",
        },
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
    }
    validation = validate_report_semantics(report)
    report["semantic_validation"] = validation
    report["publishable"] = validation["status"] == "PASS"
    if validation["status"] != "PASS":
        report["pipeline_status"] = "FAILED_VALIDATION"
    report["first_page"] = build_first_page(report)
    return report


def build_first_page(report: Mapping[str, Any]) -> dict[str, Any]:
    clusters = [item for item in report.get("correlation_clusters") or [] if isinstance(item, Mapping)]
    findings = [item for item in report.get("findings") or [] if isinstance(item, Mapping)]
    symptom = report.get("symptom_assessment") or {}
    visibility = report.get("visibility") or {}

    top_abnormal: list[dict[str, Any]] = []
    for cluster in clusters:
        top_abnormal.append({
            "id": cluster.get("cluster_id"),
            "type": cluster.get("type"),
            "time": cluster.get("representative_time"),
            "boundary": cluster.get("interpretation_boundary"),
        })
    for finding in findings:
        if str(finding.get("class") or finding.get("kind") or "ABNORMAL").upper() != "ABNORMAL":
            continue
        if finding.get("absorbed_by_cluster"):
            continue
        top_abnormal.append({
            "id": finding.get("finding_id"),
            "type": finding.get("type"),
            "severity": finding.get("severity"),
        })

    if clusters and any(str(item.get("type") or "").upper() == "CROSS_LAYER_MEDIA_TIMING_SPIKE" for item in clusters):
        conclusion = "检测到跨层媒体 timing spike；当前相关性不等于丢包或已确认根因。"
    elif top_abnormal:
        conclusion = f"检测到 {len(top_abnormal)} 个主要异常单元；结论仅覆盖当前已绑定证据。"
    else:
        conclusion = "当前已绑定证据未形成主要异常 Finding；不代表未采集范围正常。"

    symptom_reproduced = symptom.get("reproduced")
    if symptom_reproduced is True:
        symptom_text = "已复现"
    elif symptom_reproduced is False:
        symptom_text = "本次未复现"
    else:
        symptom_text = "无法确认"

    boundaries = [
        f"End-to-End media visibility: {visibility.get('end_to_end_media') or 'UNKNOWN'}",
        f"Termination: {visibility.get('termination') or 'UNKNOWN'}",
        f"Root Cause readiness: {visibility.get('root_cause_readiness') or 'INSUFFICIENT'}",
    ]
    return {
        "conclusion": conclusion,
        "symptom_reproduction": symptom_text,
        "symptom_detail": symptom.get("detail"),
        "top_abnormal": top_abnormal,
        "normal_and_exclusion": list(report.get("normal_evidence") or []) + list(report.get("exclusion_evidence") or []),
        "evidence_boundaries": boundaries,
        "next_steps": [item.get("action") for item in report.get("recommendations") or [] if item.get("action")],
    }
