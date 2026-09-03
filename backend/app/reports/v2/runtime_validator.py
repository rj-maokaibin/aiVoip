from __future__ import annotations

from typing import Any, Mapping

from .semantic_validator import validate_report_semantics


def validate_runtime_report_semantics(report: Mapping[str, Any]) -> dict[str, Any]:
    """Apply full R001-R015 validation with corrected partial-cluster R015 semantics.

    This compatibility wrapper is temporary while stacked PRs carry different
    revisions of the M2 validator. It removes only R015 violations that arise when
    a Finding contains both clustered and unclustered events. Fully represented
    but unabsorbed Findings remain a P0 violation.
    """

    result = validate_report_semantics(report)
    violations = []
    for violation in result.get("violations") or []:
        if not isinstance(violation, Mapping) or violation.get("rule") != "R015":
            violations.append(dict(violation))
            continue
        path = str(violation.get("path") or "")
        index = _finding_index(path)
        findings = report.get("findings") or []
        finding = findings[index] if index is not None and index < len(findings) else None
        if not isinstance(finding, Mapping) or _fully_represented_by_one_cluster(finding, report):
            violations.append(dict(violation))

    return {
        "status": "FAIL" if violations else "PASS",
        "ruleset": "preliminary-evidence-v2-r001-r015",
        "violations": violations,
    }


def _fully_represented_by_one_cluster(finding: Mapping[str, Any], report: Mapping[str, Any]) -> bool:
    event_to_cluster: dict[str, str] = {}
    for cluster in report.get("correlation_clusters") or []:
        if not isinstance(cluster, Mapping):
            continue
        cluster_id = str(cluster.get("cluster_id") or "")
        for member in cluster.get("member_events") or []:
            if isinstance(member, Mapping) and member.get("event_ref"):
                event_to_cluster[str(member["event_ref"])] = cluster_id
    refs = [str(ref) for ref in finding.get("event_refs") or []]
    mapped = [event_to_cluster[ref] for ref in refs if ref in event_to_cluster]
    return bool(refs) and len(mapped) == len(refs) and len(set(mapped)) == 1


def _finding_index(path: str) -> int | None:
    prefix = "findings["
    if not path.startswith(prefix):
        return None
    raw = path[len(prefix):].split("]", 1)[0]
    try:
        return int(raw)
    except ValueError:
        return None
