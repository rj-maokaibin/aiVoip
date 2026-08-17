from __future__ import annotations

from collections import defaultdict
from itertools import combinations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.contracts.evidence_report import SEVERITY_ORDER, EvidenceReportStatus
from app.db.evidence_report_models import EvidenceFinding, PreliminaryEvidenceReport

AGGREGATION_CONTRACT_VERSION = "evidence-aggregation-v1"
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


def _environment_groups(db: Session, reports: list[PreliminaryEvidenceReport]) -> list[dict]:
    by_fp: dict[str, list[PreliminaryEvidenceReport]] = defaultdict(list)
    for report in reports:
        fp = report.environment_fingerprint or "UNKNOWN_ENVIRONMENT"
        by_fp[fp].append(report)
    groups = []
    for fp, rows in sorted(by_fp.items(), key=lambda kv: kv[0]):
        summary = _summary_for_reports(db, rows)
        groups.append({
            "environment_fingerprint": fp,
            "environment": rows[-1].environment_json or {},
            "call_count": len(rows),
            "call_ids": [r.scope_id for r in rows],
            "finding_groups": summary["finding_groups"],
        })
    return groups


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
        comparisons.append({
            "environment_a": a["environment_fingerprint"],
            "environment_b": b["environment_fingerprint"],
            "environment_a_calls": a.get("call_count", 0),
            "environment_b_calls": b.get("call_count", 0),
            "rule": {
                "absolute_rate_threshold": AB_FINDING_RATE_ABS_THRESHOLD,
                "minimum_calls_per_environment": AB_MIN_CALLS_PER_ENVIRONMENT,
            },
            "differences": diffs,
        })
    return comparisons


def enrich_aggregate_payload(db: Session, *, payload: dict, scope_type: str,
                             case_id: str, session_id: str | None = None) -> dict:
    if scope_type == "CALL":
        payload["multi_call_summary"] = None
        payload["environment_groups"] = []
        payload["ab_comparison"] = []
        return payload
    reports = _current_call_reports(db, case_id=case_id, session_id=session_id if scope_type == "SESSION" else None)
    payload["multi_call_summary"] = _summary_for_reports(db, reports)
    if scope_type == "CASE":
        groups = _environment_groups(db, reports)
        payload["environment_groups"] = groups
        payload["ab_comparison"] = _ab_comparison(groups)
    else:
        payload["environment_groups"] = []
        payload["ab_comparison"] = []
    return payload
