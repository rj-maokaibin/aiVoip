from __future__ import annotations

from math import isclose
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
    """Create deterministic cross-layer correlation candidates.

    Correlation is based on shared call scope, event family, compatible media
    path and temporal proximity. ``media_path`` may be absent while upstream
    mapping is incomplete; when both events declare a path, those paths must
    match. A cluster records co-occurrence only and never asserts causality.

    Cross-layer means different media families (for example PCM + RTP), not just
    two taps inside one family. PCM_RX + PCM_TX alone therefore remains local
    evidence and is not promoted into a cross-layer media cluster.
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
            if not _compatible_media_path(anchor, candidate):
                continue
            if not _within_temporal_window(
                anchor_time,
                float(candidate["timestamp"]),
                threshold_seconds=threshold_seconds,
            ):
                continue
            members.append(candidate)

        layers = {str(member.get("layer") or "").upper() for member in members}
        layer_families = {_media_layer_family(layer) for layer in layers}
        if len(layer_families) < 2:
            continue

        members.sort(key=lambda event: (float(event["timestamp"]), str(event.get("event_id") or "")))
        member_ids = [str(member["event_id"]) for member in members]
        cluster_id = f"CC-{cluster_number:03d}"
        cluster_number += 1
        consumed.update(member_ids)

        representative = min(float(member["timestamp"]) for member in members)
        if anchor_family == "TIMING":
            cluster_type = "CROSS_LAYER_MEDIA_TIMING_SPIKE"
            interpretation_boundary = "TIMING_CORRELATION_ONLY"
        elif anchor_family == "LOSS":
            cluster_type = "CROSS_LAYER_MEDIA_LOSS_CORRELATION"
            interpretation_boundary = "LOSS_CORRELATION_ONLY"
        else:
            cluster_type = f"CROSS_LAYER_{anchor_family}"
            interpretation_boundary = "CORRELATION_ONLY"

        clusters.append(
            {
                "cluster_id": cluster_id,
                "type": cluster_type,
                "call_id": anchor_call,
                "event_family": anchor_family,
                "representative_time": representative,
                "member_events": [
                    {
                        "layer": str(member.get("layer") or "").upper(),
                        "event_ref": str(member["event_id"]),
                    }
                    for member in members
                ],
                "member_layer_families": sorted(layer_families),
                "packet_loss_observed": anchor_family == "LOSS",
                "interpretation_boundary": interpretation_boundary,
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


def _compatible_media_path(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    a_path = a.get("media_path")
    b_path = b.get("media_path")
    if a_path and b_path:
        return str(a_path) == str(b_path)
    return True


def _media_layer_family(layer: str) -> str:
    value = str(layer or "").upper()
    if value.startswith("PCM_"):
        return "PCM"
    if value.startswith("RTP"):
        return "RTP"
    return value


def _within_temporal_window(a: float, b: float, *, threshold_seconds: float) -> bool:
    """Inclusive temporal threshold with protection against float boundary drift."""

    distance = abs(float(a) - float(b))
    return distance <= threshold_seconds or isclose(
        distance,
        threshold_seconds,
        rel_tol=0.0,
        abs_tol=1e-12,
    )


def absorb_member_findings(
    findings: Iterable[Mapping[str, Any]],
    clusters: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Mark lower-level findings represented by a cross-layer primary unit."""

    event_to_cluster: dict[str, str] = {}
    for cluster in clusters:
        cluster_id = str(cluster.get("cluster_id") or "")
        for member in cluster.get("member_events") or []:
            if isinstance(member, Mapping) and member.get("event_ref"):
                event_to_cluster[str(member["event_ref"])] = cluster_id

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
    """Count one primary problem per cluster plus unabsorbed ABNORMAL findings."""

    from .finding_events import problem_count

    cluster_list = list(clusters)
    normalized_findings = absorb_member_findings(findings, cluster_list)
    return len(cluster_list) + problem_count(normalized_findings)
