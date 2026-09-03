from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable, Mapping


ABNORMAL_CLASSES = {"ABNORMAL"}
NON_PROBLEM_CLASSES = {"NORMAL", "EXCLUSION", "UNCERTAIN", "EVIDENCE_QUALITY"}
TIMING_OBSERVATIONS = {
    "PACKET_INTERVAL_SPIKE",
    "BURST_AFTER_DELAY",
    "RTP_HIGH_DELTA",
    "PCM_PACKET_INTERVAL_SPIKE",
}
LOSS_OBSERVATIONS = {
    "PACKET_SEQUENCE_LOSS",
    "RTP_SEQUENCE_LOSS",
    "PCM_SAMPLE_LOSS",
}


def build_event(
    *,
    event_id: str,
    observation_type: str,
    timestamp: float,
    layer: str,
    source_ref: str,
    call_id: str | None = None,
    direction: str | None = None,
    metrics: Mapping[str, Any] | None = None,
    evidence_refs: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Create a first-class deterministic observation event.

    Event type states what was actually measured. A timing observation is not
    silently promoted into a loss observation; causality and physical root
    cause remain outside this layer.
    """

    observation = str(observation_type).upper()
    return {
        "event_id": event_id,
        "observation_type": observation,
        "event_family": observation_family(observation),
        "timestamp": float(timestamp),
        "layer": str(layer).upper(),
        "source_ref": source_ref,
        "call_id": call_id,
        "direction": direction,
        "metrics": dict(metrics or {}),
        "evidence_refs": list(evidence_refs or []),
        "instantaneous": True,
    }


def observation_family(observation_type: str) -> str:
    observation = str(observation_type).upper()
    if observation in TIMING_OBSERVATIONS:
        return "TIMING"
    if observation in LOSS_OBSERVATIONS:
        return "LOSS"
    if observation in {"SILENCE", "ENERGY_DROP", "CLIPPING", "CLICK_POP"}:
        return "AUDIO"
    if observation.startswith("DTMF_"):
        return "DTMF"
    return observation


def aggregate_events(
    events: Iterable[Mapping[str, Any]],
    *,
    finding_id: str,
    finding_type: str,
    severity: str,
    finding_class: str = "ABNORMAL",
    title: str | None = None,
) -> dict[str, Any]:
    """Aggregate discrete events while preserving each event timestamp.

    Multiple instantaneous observations stay discrete by default. Report
    renderers must not turn the span between them into a continuous anomaly.
    """

    normalized = sorted(
        (dict(event) for event in events),
        key=lambda event: (float(event["timestamp"]), str(event.get("event_id") or "")),
    )
    timestamps = [float(event["timestamp"]) for event in normalized]
    event_refs = [str(event["event_id"]) for event in normalized]
    evidence_refs: list[str] = []
    for event in normalized:
        for ref in event.get("evidence_refs") or []:
            if ref not in evidence_refs:
                evidence_refs.append(ref)

    return {
        "finding_id": finding_id,
        "type": str(finding_type).upper(),
        "class": str(finding_class).upper(),
        "severity": str(severity).upper(),
        "title": title or str(finding_type).replace("_", " ").title(),
        "event_refs": event_refs,
        "event_count": len(normalized),
        "events": normalized,
        "time_span": {
            "start": min(timestamps) if timestamps else None,
            "end": max(timestamps) if timestamps else None,
        },
        "continuous": False,
        "evidence_refs": evidence_refs,
        "absorbed_by_cluster": None,
    }


def problem_count(findings: Iterable[Mapping[str, Any]]) -> int:
    """Count primary ABNORMAL findings only.

    NORMAL/EXCLUSION/UNCERTAIN/EVIDENCE_QUALITY findings and findings absorbed
    into a cross-layer primary cluster are not user-visible problem units.
    Severity remains independent from ``class`` as required by the V2 SPEC.
    """

    count = 0
    for finding in findings:
        finding_class = str(finding.get("class") or finding.get("kind") or "ABNORMAL").upper()
        if finding_class not in ABNORMAL_CLASSES:
            continue
        if finding.get("absorbed_by_cluster"):
            continue
        count += 1
    return count


def group_events_by_finding_key(
    events: Iterable[Mapping[str, Any]],
) -> dict[tuple[str | None, str, str, str | None], list[dict[str, Any]]]:
    """Stable helper for composers that still receive a flat event stream."""

    grouped: dict[tuple[str | None, str, str, str | None], list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        item = dict(event)
        key = (
            item.get("call_id"),
            str(item.get("observation_type") or "UNKNOWN").upper(),
            str(item.get("layer") or "UNKNOWN").upper(),
            item.get("direction"),
        )
        grouped[key].append(item)
    for value in grouped.values():
        value.sort(key=lambda item: float(item["timestamp"]))
    return dict(grouped)
