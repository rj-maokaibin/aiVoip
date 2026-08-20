from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any


DIAGNOSTIC_EVENT_SCHEMA_VERSION = "diagnostic-event-v1"
CANDIDATE_DECISION_SCHEMA_VERSION = "candidate-decision-v2"
FINDING_DIAGNOSTIC_LINK_VERSION = "finding-diagnostic-link-v1"


class CandidateDecisionStatus(StrEnum):
    """Canonical disposition of a detector/correlation candidate.

    ACCEPT
        Candidate has enough deterministic evidence to be eligible for a Finding.
    SUPPRESS
        Candidate matched a deterministic negative control / normal transient.
    INCONCLUSIVE
        Candidate may be abnormal, but required evidence is missing or weak.
    MERGE
        Candidate is valid but belongs to an already accepted same-problem Finding.
    """

    ACCEPT = "ACCEPT"
    SUPPRESS = "SUPPRESS"
    INCONCLUSIVE = "INCONCLUSIVE"
    MERGE = "MERGE"


LEGACY_DECISION_STATUS = {
    CandidateDecisionStatus.ACCEPT.value: "PROMOTED",
    CandidateDecisionStatus.SUPPRESS.value: "REJECTED_NEGATIVE_CONTROL",
    CandidateDecisionStatus.INCONCLUSIVE.value: "INCONCLUSIVE",
    CandidateDecisionStatus.MERGE.value: "PROMOTED",
}


class DiagnosticContractError(ValueError):
    pass


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _stable_id(prefix: str, value: Any, *, length: int = 20) -> str:
    digest = hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()[:length]
    return f"{prefix}-{digest}"


def _time_range(value: dict | None) -> dict:
    raw = value or {}
    start = raw.get("start")
    if start is None:
        start = raw.get("start_time")
    if start is None:
        start = raw.get("time")
    end = raw.get("end")
    if end is None:
        end = raw.get("end_time")
    if end is None:
        end = start
    representative = raw.get("representative")
    if representative is None:
        representative = raw.get("representative_time")
    if representative is None:
        representative = start

    time_base = str(raw.get("time_base") or "")
    if not time_base:
        try:
            time_base = "UNIX_EPOCH_SECONDS" if start is not None and float(start) >= 1_000_000_000 else "ANALYZER_SECONDS"
        except (TypeError, ValueError):
            time_base = "UNKNOWN"

    call_relative = raw.get("call_relative")
    if not isinstance(call_relative, dict):
        call_relative = {}
    return {
        "start": start,
        "end": end,
        "representative": representative,
        "time_base": time_base,
        "call_relative": call_relative,
    }


def _scope(value: dict | None) -> dict:
    raw = value or {}
    canonical = {
        "case_id": raw.get("case_id"),
        "session_id": raw.get("session_id"),
        "call_id": raw.get("call_id"),
        "stream_id": raw.get("stream_id") or raw.get("rtp_stream_id"),
        "layer": raw.get("layer"),
        "tap_point": raw.get("tap_point") or raw.get("pcm_tap"),
        "direction": raw.get("direction") or raw.get("rtp_direction") or raw.get("pcm_direction"),
        "ssrc": raw.get("ssrc"),
        "path_role": raw.get("path_role") or raw.get("stream_role"),
    }
    # Preserve domain-specific fields without making them identity requirements.
    extras = {k: v for k, v in raw.items() if k not in canonical and v is not None}
    canonical["extensions"] = extras
    return canonical


def build_diagnostic_event(
    *,
    event_type: str,
    analyzer_id: str,
    analyzer_version: str | None = None,
    profile_version: str | None = None,
    profile_checksum: str | None = None,
    scope: dict | None = None,
    time_range: dict | None = None,
    measurements: dict | None = None,
    thresholds: dict | None = None,
    context: dict | None = None,
    negative_conditions: list | None = None,
    evidence_refs: list | None = None,
    packet_refs: list | None = None,
    quality: dict | None = None,
    source_ref: dict | None = None,
    event_id: str | None = None,
) -> dict:
    canonical_scope = _scope(scope)
    canonical_time = _time_range(time_range)
    source_ref = dict(source_ref or {})
    stable_material = {
        "event_type": str(event_type),
        "analyzer_id": str(analyzer_id),
        "scope": canonical_scope,
        "time_range": canonical_time,
        "source_ref": source_ref,
    }
    item = {
        "schema_version": DIAGNOSTIC_EVENT_SCHEMA_VERSION,
        "event_id": event_id or _stable_id("event", stable_material),
        "event_type": str(event_type),
        "analyzer": {
            "id": str(analyzer_id),
            "version": analyzer_version,
            "profile_version": profile_version,
            "profile_checksum": profile_checksum,
        },
        "scope": canonical_scope,
        "time_range": canonical_time,
        "measurements": dict(measurements or {}),
        "thresholds": dict(thresholds or {}),
        "context": dict(context or {}),
        "negative_conditions": list(negative_conditions or []),
        "evidence_refs": list(evidence_refs or []),
        "packet_refs": list(packet_refs or []),
        "quality": dict(quality or {}),
        "source_ref": source_ref,
    }
    validate_diagnostic_event(item)
    return item


def validate_diagnostic_event(item: dict) -> None:
    if item.get("schema_version") != DIAGNOSTIC_EVENT_SCHEMA_VERSION:
        raise DiagnosticContractError("DIAGNOSTIC_EVENT_SCHEMA_UNSUPPORTED")
    if not str(item.get("event_id") or ""):
        raise DiagnosticContractError("DIAGNOSTIC_EVENT_ID_REQUIRED")
    if not str(item.get("event_type") or ""):
        raise DiagnosticContractError("DIAGNOSTIC_EVENT_TYPE_REQUIRED")
    analyzer = item.get("analyzer") or {}
    if not str(analyzer.get("id") or ""):
        raise DiagnosticContractError("DIAGNOSTIC_EVENT_ANALYZER_REQUIRED")
    tr = item.get("time_range") or {}
    if not {"start", "end", "representative", "time_base"}.issubset(tr):
        raise DiagnosticContractError("DIAGNOSTIC_EVENT_TIME_RANGE_INVALID")


def build_candidate_decision(
    event: dict,
    *,
    status: CandidateDecisionStatus | str,
    reason_code: str,
    rule_version: str,
    reason: str | None = None,
    negative_conditions: list | None = None,
    positive_evidence: dict | None = None,
    related_event_refs: list | None = None,
    merge_target_event_id: str | None = None,
    legacy_status: str | None = None,
) -> dict:
    validate_diagnostic_event(event)
    canonical_status = status.value if isinstance(status, CandidateDecisionStatus) else str(status).upper()
    if canonical_status not in {x.value for x in CandidateDecisionStatus}:
        raise DiagnosticContractError(f"CANDIDATE_DECISION_STATUS_INVALID:{canonical_status}")
    if canonical_status == CandidateDecisionStatus.MERGE.value and not merge_target_event_id:
        raise DiagnosticContractError("CANDIDATE_DECISION_MERGE_TARGET_REQUIRED")
    material = {
        "event_id": event["event_id"],
        "status": canonical_status,
        "reason_code": reason_code,
        "rule_version": rule_version,
        "merge_target_event_id": merge_target_event_id,
    }
    item = {
        "schema_version": CANDIDATE_DECISION_SCHEMA_VERSION,
        "decision_id": _stable_id("decision", material),
        "event_id": event["event_id"],
        "event_type": event["event_type"],
        "status": canonical_status,
        "legacy_status": legacy_status or LEGACY_DECISION_STATUS[canonical_status],
        "reason_code": str(reason_code),
        "reason": reason,
        "rule_version": str(rule_version),
        "negative_conditions": list(negative_conditions or []),
        "positive_evidence": dict(positive_evidence or {}),
        "related_event_refs": list(related_event_refs or []),
        "merge_target_event_id": merge_target_event_id,
    }
    validate_candidate_decision(item)
    return item


def validate_candidate_decision(item: dict) -> None:
    if item.get("schema_version") != CANDIDATE_DECISION_SCHEMA_VERSION:
        raise DiagnosticContractError("CANDIDATE_DECISION_SCHEMA_UNSUPPORTED")
    if not str(item.get("decision_id") or "") or not str(item.get("event_id") or ""):
        raise DiagnosticContractError("CANDIDATE_DECISION_REFERENCE_REQUIRED")
    if str(item.get("status") or "") not in {x.value for x in CandidateDecisionStatus}:
        raise DiagnosticContractError("CANDIDATE_DECISION_STATUS_INVALID")
    if not str(item.get("reason_code") or "") or not str(item.get("rule_version") or ""):
        raise DiagnosticContractError("CANDIDATE_DECISION_REASON_REQUIRED")
    if item.get("status") == CandidateDecisionStatus.MERGE.value and not item.get("merge_target_event_id"):
        raise DiagnosticContractError("CANDIDATE_DECISION_MERGE_TARGET_REQUIRED")


def build_finding_diagnostic_link(*, events: list[dict], decisions: list[dict]) -> dict:
    for event in events:
        validate_diagnostic_event(event)
    event_ids = {str(x["event_id"]) for x in events}
    for decision in decisions:
        validate_candidate_decision(decision)
        if str(decision["event_id"]) not in event_ids:
            raise DiagnosticContractError("FINDING_DECISION_EVENT_NOT_FOUND")

    by_status = {status.value: [] for status in CandidateDecisionStatus}
    for decision in decisions:
        by_status[str(decision["status"])].append(str(decision["event_id"]))
    return {
        "schema_version": FINDING_DIAGNOSTIC_LINK_VERSION,
        "event_ids": sorted(event_ids),
        "decision_ids": sorted(str(x["decision_id"]) for x in decisions),
        "accepted_event_ids": sorted(set(by_status[CandidateDecisionStatus.ACCEPT.value])),
        "suppressed_event_ids": sorted(set(by_status[CandidateDecisionStatus.SUPPRESS.value])),
        "inconclusive_event_ids": sorted(set(by_status[CandidateDecisionStatus.INCONCLUSIVE.value])),
        "merged_event_ids": sorted(set(by_status[CandidateDecisionStatus.MERGE.value])),
        "events": list(events),
        "decisions": list(decisions),
    }


def validate_finding_diagnostic_link(item: dict, *, require_accepted: bool = True) -> None:
    if item.get("schema_version") != FINDING_DIAGNOSTIC_LINK_VERSION:
        raise DiagnosticContractError("FINDING_DIAGNOSTIC_LINK_SCHEMA_UNSUPPORTED")
    events = item.get("events") or []
    decisions = item.get("decisions") or []
    rebuilt = build_finding_diagnostic_link(events=events, decisions=decisions)
    if sorted(item.get("event_ids") or []) != rebuilt["event_ids"]:
        raise DiagnosticContractError("FINDING_DIAGNOSTIC_EVENT_IDS_MISMATCH")
    if require_accepted and not rebuilt["accepted_event_ids"]:
        raise DiagnosticContractError("FINDING_WITHOUT_ACCEPTED_DIAGNOSTIC_EVENT")
