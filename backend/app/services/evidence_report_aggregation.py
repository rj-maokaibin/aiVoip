from __future__ import annotations

from collections import defaultdict
from itertools import combinations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.contracts.evidence_report import SEVERITY_ORDER, EvidenceReportStatus
from app.db.evidence_report_models import EvidenceFinding, PreliminaryEvidenceReport
from app.reports.prd_spec_v1_alignment import average_metric_rows, scalar_media_metrics

AGGREGATION_CONTRACT_VERSION = "evidence-aggregation-v2"
NORMAL_BASELINE_CONTRACT_VERSION = "normal-baseline-v1"
AB_FINDING_RATE_ABS_THRESHOLD = 0.50
AB_MIN_CALLS_PER_ENVIRONMENT = 2


_ACTIVE_REPORT_STATES = {
    EvidenceReportStatus.COMPLETE.value,
    EvidenceReportStatus.PARTIAL_COMPLETE.value,
}


def _current_call_reports(db: Session, *, case_id: str, session_id: str | None = None) -> list[PreliminaryEvidenceReport]:
    stmt = select(PreliminaryEvidenceReport).where(
        PreliminaryEvidenceReport.case_id == case_id,
        PreliminaryEvidenceReport.scope_type == "CALL",
        PreliminaryEvidenceReport.status.in_(_ACTIVE_REPORT_STATES),
    )
    if session_id:
        stmt = stmt.where(PreliminaryEvidenceReport.session_id == session_id)
    return list(db.scalars(stmt.order_by(PreliminaryEvidenceReport.created_at.asc())))


def _finding_rows(db: Session, reports: list[PreliminaryEvidenceReport]) -> dict[str, list[EvidenceFinding]]:
    if not reports:
        return {}
    call_ids = [r.scope_id for r in reports]
    rows = list(db.scalars(select(EvidenceFinding).where(
        EvidenceFinding.scope_type == "CALL",
        EvidenceFinding.scope_id.in_(call_ids),
        EvidenceFinding.status.not_in(["RESOLVED", "INVALIDATED"]),
    )))
    out: dict[str, list[EvidenceFinding]] = defaultdict(list)
    for row in rows:
        out[row.scope_id].append(row)
    return out


def _summary_for_reports(db: Session, reports: list[PreliminaryEvidenceReport]) -> dict:
    by_call = _finding_rows(db, reports)
    grouped: dict[str, dict] = {}
    for report in reports:
        seen: set[str] = set()
        for finding in by_call.get(report.scope_id, []):
            signature = finding.finding_signature
            if signature in seen:
                continue
            seen.add(signature)
            item = grouped.setdefault(signature, {
                "finding_signature": signature,
                "finding_type": finding.finding_type,
                "title": finding.title,
                "severity": finding.severity,
                "evidence_level": finding.evidence_level,
                "call_ids": [],
                "report_ids": [],
            })
            item["call_ids"].append(report.scope_id)
            item["report_ids"].append(report.id)
            if SEVERITY_ORDER.get(finding.severity, 0) > SEVERITY_ORDER.get(item["severity"], 0):
                item["severity"] = finding.severity
            if str(finding.evidence_level) < str(item["evidence_level"]):
                item["evidence_level"] = finding.evidence_level
    total = len(reports)
    items = []
    for item in grouped.values():
        calls = sorted(set(item.pop("call_ids")))
        item["call_ids"] = calls
        item["occurrence_calls"] = len(calls)
        item["total_calls"] = total
        item["reproduction_rate"] = round(len(calls) / total, 6) if total else None
        item["stability"] = (
            "STABLE" if total >= 2 and len(calls) == total
            else "FREQUENT" if total >= 3 and len(calls) / total >= 0.67
            else "INTERMITTENT" if len(calls) else "NOT_OBSERVED"
        )
        items.append(item)
    items.sort(key=lambda x: (-SEVERITY_ORDER.get(x.get("severity", "INFO"), 0), -(x.get("reproduction_rate") or 0), x.get("title") or ""))
    return {
        "contract_version": AGGREGATION_CONTRACT_VERSION,
        "call_count": total,
        "finding_groups": items,
    }


def _snapshot(report: PreliminaryEvidenceReport) -> dict:
    return report.snapshot_json if isinstance(report.snapshot_json, dict) else {}


def _environment_groups(db: Session, reports: list[PreliminaryEvidenceReport]) -> list[dict]:
    by_fp: dict[str, list[PreliminaryEvidenceReport]] = defaultdict(list)
    for report in reports:
        fp = report.environment_fingerprint or "UNKNOWN_ENVIRONMENT"
        by_fp[fp].append(report)
    groups = []
    for fp, rows in sorted(by_fp.items(), key=lambda kv: kv[0]):
        summary = _summary_for_reports(db, rows)
        snapshots = [_snapshot(r) for r in rows]
        groups.append({
            "environment_fingerprint": fp,
            "environment": rows[-1].environment_json or {},
            "call_count": len(rows),
            "call_ids": [r.scope_id for r in rows],
            "report_ids": [r.id for r in rows],
            "finding_groups": summary["finding_groups"],
            "metric_summary": average_metric_rows(snapshots),
            "evidence_boundaries": [
                ((_snapshot(r).get("evidence_boundary") or {}).get("statement"))
                for r in rows
                if ((_snapshot(r).get("evidence_boundary") or {}).get("statement"))
            ],
        })
    return groups


def _metric_differences(a: dict, b: dict) -> list[dict]:
    labels = {
        "rtp_loss_rate_mean": ("NETWORK_MEDIA", "RTP 平均丢包率"),
        "rtp_p95_jitter_ms_mean": ("NETWORK_MEDIA", "RTP P95 抖动"),
        "rtp_max_delta_ms_mean": ("NETWORK_MEDIA", "RTP 最大 Delta"),
        "pcm_rms_dbfs_mean": ("DIGITAL_LEVEL", "PCM RMS dBFS"),
        "pcm_peak_dbfs_mean": ("DIGITAL_LEVEL", "PCM Peak dBFS"),
        "spectrum_periodic_score_mean": ("SPECTRUM", "周期频谱得分"),
    }
    out = []
    am = a.get("metric_summary") or {}; bm = b.get("metric_summary") or {}
    for key, (dimension, label) in labels.items():
        av = am.get(key); bv = bm.get(key)
        out.append({
            "metric": key,
            "dimension": dimension,
            "label": label,
            "environment_a_value": av,
            "environment_b_value": bv,
            "delta": round(float(bv) - float(av), 6) if av is not None and bv is not None else None,
            "status": "COMPARABLE" if av is not None and bv is not None else "INSUFFICIENT_EVIDENCE",
        })
    return out


def _ab_comparison(groups: list[dict]) -> list[dict]:
    comparisons = []
    for a, b in combinations(groups, 2):
        a_map = {x["finding_signature"]: x for x in a.get("finding_groups", [])}
        b_map = {x["finding_signature"]: x for x in b.get("finding_groups", [])}
        diffs = []
        for signature in sorted(set(a_map) | set(b_map)):
            av = a_map.get(signature); bv = b_map.get(signature)
            ar = float((av or {}).get("reproduction_rate") or 0.0)
            br = float((bv or {}).get("reproduction_rate") or 0.0)
            delta = round(br - ar, 6)
            enough_repeats = a.get("call_count", 0) >= AB_MIN_CALLS_PER_ENVIRONMENT and b.get("call_count", 0) >= AB_MIN_CALLS_PER_ENVIRONMENT
            significant = enough_repeats and abs(delta) >= AB_FINDING_RATE_ABS_THRESHOLD
            source = bv or av or {}
            diffs.append({
                "finding_signature": signature,
                "finding_type": source.get("finding_type"),
                "title": source.get("title"),
                "environment_a_rate": ar,
                "environment_b_rate": br,
                "absolute_rate_delta": delta,
                "repeatability_requirement_met": enough_repeats,
                "significant_by_v1_rule": significant,
                "interpretation_boundary": "A/B Finding 复现率差异属于环境关联证据，不独立确认因果或最终根因。",
            })
        metric_differences = _metric_differences(a, b)
        comparable_dimensions = {x["dimension"] for x in metric_differences if x["status"] == "COMPARABLE"}
        comparisons.append({
            "contract_version": AGGREGATION_CONTRACT_VERSION,
            "environment_a": a["environment_fingerprint"],
            "environment_b": b["environment_fingerprint"],
            "environment_a_calls": a.get("call_count", 0),
            "environment_b_calls": b.get("call_count", 0),
            "rule": {
                "absolute_rate_threshold": AB_FINDING_RATE_ABS_THRESHOLD,
                "minimum_calls_per_environment": AB_MIN_CALLS_PER_ENVIRONMENT,
            },
            "differences": diffs,
            "metric_differences": metric_differences,
            "evidence_boundary_comparison": {
                "environment_a": a.get("evidence_boundaries") or [],
                "environment_b": b.get("evidence_boundaries") or [],
                "boundary": "A/B 仅比较各环境已观测事实和 Evidence Boundary，不把差异升级为因果或最终根因。",
            },
            "dimensions": {
                "reproduction_rate": "AVAILABLE",
                "finding": "AVAILABLE",
                "network_media": "AVAILABLE" if "NETWORK_MEDIA" in comparable_dimensions else "INSUFFICIENT_EVIDENCE",
                "pcm": "AVAILABLE" if {"DIGITAL_LEVEL", "SPECTRUM"} & comparable_dimensions else "INSUFFICIENT_EVIDENCE",
                "digital_level": "AVAILABLE" if "DIGITAL_LEVEL" in comparable_dimensions else "INSUFFICIENT_EVIDENCE",
                "spectrum": "AVAILABLE" if "SPECTRUM" in comparable_dimensions else "INSUFFICIENT_EVIDENCE",
                "evidence_boundary": "AVAILABLE",
            },
        })
    return comparisons


def _normal_baseline_comparison(db: Session, *, payload: dict, reports: list[PreliminaryEvidenceReport], current_scope_id: str | None) -> dict:
    """FR-019: compare only against exact-environment normal reports; fail closed otherwise."""
    fp = payload.get("environment_fingerprint")
    if not fp:
        return {
            "contract_version": NORMAL_BASELINE_CONTRACT_VERSION,
            "status": "NOT_MATCHED",
            "reason": "CURRENT_ENVIRONMENT_FINGERPRINT_MISSING",
            "boundary": "当前环境指纹缺失，不强行使用正常基线。",
        }
    candidates = []
    for report in reports:
        if report.scope_id == current_scope_id or report.environment_fingerprint != fp:
            continue
        snap = _snapshot(report)
        if int(snap.get("finding_count") or len(snap.get("findings") or [])) != 0:
            continue
        if not (snap.get("normal_evidence") or snap.get("normal_and_exclusion_evidence")):
            continue
        candidates.append(report)
    if not candidates:
        return {
            "contract_version": NORMAL_BASELINE_CONTRACT_VERSION,
            "status": "NOT_MATCHED",
            "environment_fingerprint": fp,
            "reason": "NO_EXACT_ENVIRONMENT_NORMAL_BASELINE",
            "boundary": "没有满足最低匹配条件的同环境正常 Call，不强行使用基线。",
        }
    baseline_metrics = average_metric_rows([_snapshot(r) for r in candidates])
    current_metrics = scalar_media_metrics(payload)
    metric_differences = []
    for key in baseline_metrics:
        base = baseline_metrics.get(key); current = current_metrics.get(key)
        metric_differences.append({
            "metric": key,
            "baseline": base,
            "current": current,
            "delta": round(float(current) - float(base), 6) if current is not None and base is not None else None,
            "status": "COMPARABLE" if current is not None and base is not None else "INSUFFICIENT_EVIDENCE",
        })
    return {
        "contract_version": NORMAL_BASELINE_CONTRACT_VERSION,
        "status": "MATCHED",
        "environment_fingerprint": fp,
        "minimum_match_rule": "EXACT_ENVIRONMENT_FINGERPRINT_AND_ZERO_FINDING_NORMAL_EVIDENCE",
        "baseline_report_ids": [r.id for r in candidates],
        "baseline_call_ids": [r.scope_id for r in candidates],
        "baseline_count": len(candidates),
        "metric_differences": metric_differences,
        "boundary": "正常基线仅作为同环境对照证据；差异不独立确认异常原因或最终根因。",
    }


def enrich_aggregate_payload(db: Session, *, payload: dict, scope_type: str,
                             case_id: str, session_id: str | None = None) -> dict:
    reports = _current_call_reports(db, case_id=case_id, session_id=session_id if scope_type == "SESSION" else None)
    if scope_type == "CALL":
        payload["multi_call_summary"] = None
        payload["environment_groups"] = []
        payload["ab_comparison"] = []
        payload["normal_baseline_comparison"] = _normal_baseline_comparison(
            db, payload=payload, reports=reports, current_scope_id=str((payload.get("scope") or {}).get("id") or "")
        )
        return payload
    payload["multi_call_summary"] = _summary_for_reports(db, reports)
    if scope_type == "CASE":
        groups = _environment_groups(db, reports)
        payload["environment_groups"] = groups
        payload["ab_comparison"] = _ab_comparison(groups)
        payload["normal_baseline_groups"] = [
            {
                "environment_fingerprint": group["environment_fingerprint"],
                "normal_report_ids": [
                    r.id for r in reports
                    if r.environment_fingerprint == group["environment_fingerprint"]
                    and int(_snapshot(r).get("finding_count") or len(_snapshot(r).get("findings") or [])) == 0
                    and bool(_snapshot(r).get("normal_evidence") or _snapshot(r).get("normal_and_exclusion_evidence"))
                ],
            }
            for group in groups
        ]
    else:
        payload["environment_groups"] = []
        payload["ab_comparison"] = []
        payload["normal_baseline_comparison"] = _normal_baseline_comparison(
            db, payload=payload, reports=reports, current_scope_id=None
        )
    return payload