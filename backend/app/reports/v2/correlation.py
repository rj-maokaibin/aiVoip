from __future__ import annotations

from typing import Any, Iterable, Mapping


MEDIA_LAYERS = {
    "PCM_RX",
    "PCM_TX",
    "RTP_UPSTREAM",
    "RTP_DOWNSTREAM",
    "RTP",
}


def correlate_media_events(
    events: Iterable[Mapping[str, Any]],
    *,
    threshold_ms: float = 50.0,
) -> list[dict[str, Any]]:
    """Create deterministic cross-layer candidate clusters.

    Correlation is based on shared call scope, event family, media-layer
    compatibility and temporal proximity. A cluster records co-occurrence only;
    it never asserts physical causality or root cause.
    """

    threshold_seconds = float(threshold_ms) / 1000.0
    candidates = [
        dict(event)
        for event in events
        if str(event.get("layer") or "").upper() in MEDIA_LAYERS
        and event.get("timestamp") is not None
        and event.get("event_family")
    ]
    candidates.sort(
        key=lambda event: (
            str(event.get("call_id") or ""),
            str(event.get("event_family") or ""),
            float(event["timestamp"]),
            str(event.get("event_id") or ""),
        )
    )

    clusters: list[dict[str, Any]] = []
    consumed: set[str] = set()
    cluster_number = 1

    for anchor in candidates:
        anchor_id = str(anchor.get("event_id") or "")
        if not anchor_id or anchor_id in consumed:
            continue

        members = [anchor]
        anchor_time = float(anchor["timestamp"])
        anchor_call = anchor.get("call_id")
        anchor_family = str(anchor.get("event_family"))

        for candidate in candidates:
            candidate_id = str(candidate.get("event_id") or "")
            if not candidate_id or candidate_id == anchor_id or candidate_id in consumed:
                continue
            if candidate.get("call_id") != anchor_call:
                continue
            if str(candidate.get("event_family")) != anchor_family:
                continue
            if abs(float(candidate["timestamp"]) - anchor_time) > threshold_seconds:
                continue
            members.append(candidate)

        layers = {str(member.get("layer") or "").upper() for member in members}
        # A cross-layer cluster must contain evidence from at least two distinct
        # layers. Same-layer repeated spikes remain a normal Finding aggregation.
        if len(layers) < 2:
            continue

        members.sort(key=lambda event: (float(event["timestamp"]), str(event.get("event_id") or "")))
        member_ids = [str(member["event_id"]) for member in members]
        cluster_id = f"XLY-{cluster_number:03d}"
        cluster_number += 1
        consumed.update(member_ids)

        representative = min(float(member["timestamp"]) for member in members)
        if anchor_family == "TIMING":
            finding_type = "CROSS_LAYER_MEDIA_TIMING_SPIKE"
        elif anchor_family == "LOSS":
            finding_type = "CROSS_LAYER_MEDIA_LOSS_CORRELATION"
        else:
            finding_type = f"CROSS_LAYER_{anchor_family}"

        clusters.append(
            {
                "cluster_id": cluster_id,
                "kind": "CORRELATION_CANDIDATE",
                "finding_type": finding_type,
                "event_family": anchor_family,
                "call_id": anchor_call,
                "member_event_refs": member_ids,
                "member_layers": sorted(layers),
                "event_count": len(member_ids),
                "representative_time": representative,
                "time_span": {
                    "start": representative,
                    "end": max(float(member["timestamp"]) for member in members),
                },
                "threshold_ms": float(threshold_ms),
                "causality_confirmed": False,
                "root_cause_confirmed": False,
            }
        )

    return clusters


def absorb_member_findings(
    findings: Iterable[Mapping[str, Any]],
    clusters: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Mark lower-level findings represented by a cross-layer primary unit."""

    event_to_cluster: dict[str, str] = {}
    for cluster in clusters:
        cluster_id = str(cluster.get("cluster_id") or "")
        for event_ref in cluster.get("member_event_refs") or []:
            event_to_cluster[str(event_ref)] = cluster_id

    out: list[dict[str, Any]] = []
    for finding in findings:
        item = dict(finding)
        cluster_ids = {
            event_to_cluster[str(ref)]
            for ref in item.get("event_refs") or []
            if str(ref) in event_to_cluster
        }
        if len(cluster_ids) == 1:
            item["absorbed_by_cluster"] = next(iter(cluster_ids))
        out.append(item)
    return out


def correlation_problem_count(
    findings: Iterable[Mapping[str, Any]],
    clusters: Iterable[Mapping[str, Any]],
) -> int:
    """Count one primary problem per cluster plus unabsorbed abnormal findings."""

    from .finding_events import problem_count

    cluster_list = list(clusters)
    normalized_findings = absorb_member_findings(findings, cluster_list)
    return len(cluster_list) + problem_count(normalized_findings)
