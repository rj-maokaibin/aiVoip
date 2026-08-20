from __future__ import annotations

from app.analyzers.cross_layer import periodic_finding_observation


def apply_first_observable_boundaries(payload: dict) -> dict:
    """Attach analyzer-owned Cross-Layer Observations without adding root authority.

    Candidate-gated Findings may already carry a deterministic boundary from the
    Analyzer/Finding layer. Periodic Findings are normalized here through the
    same shared contract. Existing boundary evidence is never upgraded.
    """
    for finding in payload.get("findings", []) or []:
        correlation = dict(finding.get("correlation") or {})
        existing = correlation.get("first_observable_boundary")
        cross = correlation.get("cross_layer_observation")

        if not existing:
            periodic = periodic_finding_observation(finding)
            if periodic:
                cross = periodic
                existing = periodic.get("first_observable_boundary") or {}
                correlation["cross_layer_observation"] = periodic
                correlation["first_observable_boundary"] = existing

        if not existing:
            continue

        correlation["role_boundary"] = (
            "Cross-Layer 角色来自当前 Analyzer/Correlation 的确定性方向或 Tap 映射；"
            "该字段只描述当前证据链中的首次可观测位置，不声明异常物理起源。"
        )
        finding["correlation"] = correlation

        status = existing.get("status")
        statement = str(existing.get("statement") or "")
        interpretation = str(finding.get("interpretation") or "").strip()
        if status == "OBSERVED_BOUNDARY" and statement and statement not in interpretation:
            finding["interpretation"] = (interpretation + " " + statement).strip()
        elif status == "UNKNOWN" and "首次异常层保持 UNKNOWN" not in interpretation:
            finding["interpretation"] = (
                interpretation + " 当前上游/对照层证据不完整，首次异常层保持 UNKNOWN（未知）。"
            ).strip()
    return payload
