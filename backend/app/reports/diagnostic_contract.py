from __future__ import annotations

from collections import Counter
from copy import deepcopy
from typing import Any

from app.contracts.diagnostic import (
    CANDIDATE_DECISION_SCHEMA_VERSION,
    DIAGNOSTIC_EVENT_SCHEMA_VERSION,
    FINDING_DIAGNOSTIC_LINK_VERSION,
    CandidateDecisionStatus,
    DiagnosticContractError,
    build_candidate_decision,
    build_diagnostic_event,
    build_finding_diagnostic_link,
    validate_candidate_decision,
    validate_diagnostic_event,
    validate_finding_diagnostic_link,
)


DIAGNOSTIC_CONTRACT_SNAPSHOT_VERSION = "diagnostic-contract-snapshot-v1"
DIRECT_ACCEPT_RULE_VERSION = "diagnostic-direct-accept-v1"
LEGACY_CANDIDATE_ADAPTER_VERSION = "candidate-decision-legacy-adapter-v1"
FINDING_MERGE_RULE_VERSION = "finding-signature-merge-v1"


_LEGACY_TO_CANONICAL = {
    "PROMOTED": CandidateDecisionStatus.ACCEPT.value,
    "REJECTED_NEGATIVE_CONTROL": CandidateDecisionStatus.SUPPRESS.value,
    "INCONCLUSIVE": CandidateDecisionStatus.INCONCLUSIVE.value,
}


def _state_meta(analyzer_states: dict[str, dict], analyzer_id: str) -> dict:
    state = analyzer_states.get(analyzer_id) or {}
    return {
        "analyzer_version": state.get("analyzer_version") or state.get("version"),
        "profile_version": state.get("config_version"),
        "profile_checksum": state.get("config_checksum"),
    }


def _event_time(event: dict) -> dict:
    start = event.get("start_time")
    if start is None:
        start = event.get("time")
    end = event.get("end_time")
    if end is None:
        details = event.get("details") or {}
        end = details.get("absolute_end_time")
    if end is None:
        end = start
    return {"start": start, "end": end, "representative": event.get("representative_time", start)}


def _scope_matches(event_scope: dict, finding_scope: dict) -> bool:
    if not event_scope:
        return True
    pairs = (
        (event_scope.get("call_id"), finding_scope.get("call_id")),
        (event_scope.get("stream_id"), finding_scope.get("rtp_stream_id") or finding_scope.get("stream_id")),
        (event_scope.get("tap_point"), finding_scope.get("pcm_tap") or finding_scope.get("tap_point")),
        (event_scope.get("direction"), finding_scope.get("direction") or finding_scope.get("pcm_direction") or finding_scope.get("rtp_direction")),
        (event_scope.get("ssrc"), finding_scope.get("ssrc")),
    )
    for left, right in pairs:
        if left not in (None, "") and right not in (None, "") and str(left) != str(right):
            return False
    return True


def _time_in_finding(event: dict, finding: dict) -> bool:
    et = event.get("time_range") or {}
    ft = finding.get("time_range") or {}
    er = et.get("representative")
    fs = ft.get("start")
    fe = ft.get("end")
    if er is None or fs is None:
        return True
    if fe is None:
        fe = fs
    try:
        value = float(er)
        return float(fs) - 1e-6 <= value <= float(fe) + 1e-6
    except (TypeError, ValueError):
        return True


def _direct_event(
    *,
    analyzer_id: str,
    analyzer_states: dict[str, dict],
    event_type: str,
    scope: dict,
    time_range: dict,
    measurements: dict,
    evidence_refs: list | None,
    source_ref: dict,
    severity: str | None = None,
    evidence_level: str | None = None,
    context: dict | None = None,
) -> tuple[dict, dict]:
    meta = _state_meta(analyzer_states, analyzer_id)
    event = build_diagnostic_event(
        event_type=event_type,
        analyzer_id=analyzer_id,
        scope=scope,
        time_range=time_range,
        measurements=measurements,
        context=context,
        evidence_refs=evidence_refs,
        quality={"severity": severity, "evidence_level": evidence_level},
        source_ref=source_ref,
        **meta,
    )
    decision = build_candidate_decision(
        event,
        status=CandidateDecisionStatus.ACCEPT,
        reason_code="DETERMINISTIC_ANALYZER_EVENT_ACCEPTED",
        reason="Analyzer 已按版本化确定性规则输出该事件；PR7 仅做契约投影，不提升证据等级。",
        rule_version=DIRECT_ACCEPT_RULE_VERSION,
    )
    return event, decision


def _legacy_candidate(
    raw: dict,
    *,
    analyzer_states: dict[str, dict],
) -> tuple[dict, dict]:
    source = deepcopy(raw.get("source_event") or {})
    scope = source.get("scope") or raw.get("scope") or {}
    ftype = str(source.get("type") or raw.get("candidate_type") or "UNKNOWN_CANDIDATE")
    candidate_id = str(raw.get("candidate_id") or "")
    details = source.get("details") or {}
    negative = list(raw.get("negative_controls") or [])
    meta = _state_meta(analyzer_states, "media_intelligence")
    event = build_diagnostic_event(
        event_type=ftype,
        analyzer_id="media_intelligence",
        scope=scope,
        time_range=_event_time(source or {"time": raw.get("candidate_time")}),
        measurements=details,
        negative_conditions=negative,
        context={"legacy_candidate_id": candidate_id, "raw_pcm_candidate": bool(raw.get("raw_pcm_candidate"))},
        quality={"severity": source.get("severity"), "evidence_level": source.get("evidence_level")},
        source_ref={"source": "media.candidate_decisions", "candidate_id": candidate_id},
        **meta,
    )
    legacy_status = str(raw.get("status") or "INCONCLUSIVE")
    canonical_status = _LEGACY_TO_CANONICAL.get(legacy_status, CandidateDecisionStatus.INCONCLUSIVE.value)
    positive = raw.get("positive_evidence") or raw.get("activity_evidence") or {}
    decision = build_candidate_decision(
        event,
        status=canonical_status,
        reason_code=str(raw.get("reason_code") or "LEGACY_DECISION_REASON_MISSING"),
        reason="由现有 CandidateDecision V1 无损映射为 PR7 canonical disposition。",
        rule_version=LEGACY_CANDIDATE_ADAPTER_VERSION,
        negative_conditions=negative,
        positive_evidence=positive if isinstance(positive, dict) else {},
        legacy_status=legacy_status,
    )
    decision["legacy_candidate_id"] = candidate_id or None
    return event, decision


def build_diagnostic_contract_snapshot(*, results: dict[str, dict | None], analyzer_states: dict[str, dict]) -> dict:
    events: list[dict] = []
    decisions: list[dict] = []
    candidate_event_ids: dict[str, str] = {}

    media = results.get("media_intelligence") or {}
    for raw in media.get("candidate_decisions", []) or []:
        event, decision = _legacy_candidate(raw, analyzer_states=analyzer_states)
        events.append(event)
        decisions.append(decision)
        candidate_id = str(raw.get("candidate_id") or "")
        if candidate_id:
            candidate_event_ids[candidate_id] = event["event_id"]

    packet = results.get("packet_intelligence") or {}
    streams = {str(x.get("stream_id")): x for x in packet.get("rtp_streams", []) or [] if x.get("stream_id") is not None}
    for index, anomaly in enumerate(packet.get("anomalies", []) or []):
        evidence = anomaly.get("evidence") or {}
        stream = streams.get(str(evidence.get("stream_id"))) or {}
        scope = {
            "layer": "RTP" if evidence.get("stream_id") else "SIP_SDP",
            "call_id": evidence.get("call_id"),
            "rtp_stream_id": evidence.get("stream_id"),
            "direction": evidence.get("direction") or stream.get("direction") or stream.get("call_direction_role"),
            "ssrc": evidence.get("ssrc") or stream.get("ssrc"),
            "stream_role": evidence.get("stream_id"),
        }
        measurements = dict(evidence)
        if stream:
            measurements.setdefault("lost_packets", stream.get("lost_packets", stream.get("lost")))
            measurements.setdefault("loss_rate", stream.get("loss_rate"))
            measurements.setdefault("max_delta_ms", stream.get("max_delta_ms"))
            measurements.setdefault("ptime_ms", stream.get("ptime_ms"))
        event, decision = _direct_event(
            analyzer_id="packet_intelligence",
            analyzer_states=analyzer_states,
            event_type=str(anomaly.get("type") or "PACKET_ANOMALY"),
            scope=scope,
            time_range=_event_time(anomaly),
            measurements=measurements,
            evidence_refs=[{"type": "ANALYZER_RUN", "id": (analyzer_states.get("packet_intelligence") or {}).get("run_id")}],
            source_ref={"source": "packet.anomalies", "index": index},
            severity=anomaly.get("severity"),
            evidence_level="L2",
        )
        events.append(event)
        decisions.append(decision)

    pcm = results.get("pcm_intelligence") or {}
    for stream_index, stream in enumerate(pcm.get("streams", []) or []):
        tap = stream.get("tap") or {}
        tap_name = str(tap.get("name") or "pcm")
        direction = str(tap.get("direction") or "").upper() or None
        for session_index, session in enumerate(stream.get("sessions", []) or []):
            base_scope = {
                "layer": tap_name.upper(),
                "pcm_tap": tap_name,
                "pcm_direction": direction,
                "pcm_session_index": session.get("session_index"),
                "direction": direction,
            }
            session_start = float(session.get("start_time") or 0.0)
            for index, gap in enumerate(session.get("gap_events", []) or []):
                event, decision = _direct_event(
                    analyzer_id="pcm_intelligence", analyzer_states=analyzer_states, event_type="PCM_GAP",
                    scope=base_scope, time_range={"start": gap.get("time"), "end": gap.get("time"), "representative": gap.get("time")},
                    measurements=gap, evidence_refs=[],
                    source_ref={"source": "pcm.gap_events", "stream_index": stream_index, "session_index": session_index, "index": index},
                    severity="MEDIUM", evidence_level="L2",
                )
                events.append(event); decisions.append(decision)

            hum = session.get("hum") or {}
            if str(hum.get("level") or "LOW").upper() in {"MEDIUM", "HIGH"}:
                event, decision = _direct_event(
                    analyzer_id="pcm_intelligence", analyzer_states=analyzer_states, event_type="PERIODIC_LOW_FREQUENCY_INTERFERENCE",
                    scope=base_scope,
                    time_range={"start": session.get("start_time"), "end": session.get("end_time"), "representative": session.get("start_time")},
                    measurements={"hum": hum, "signal": session.get("signal") or {}}, evidence_refs=[],
                    source_ref={"source": "pcm.hum", "stream_index": stream_index, "session_index": session_index},
                    severity=str(hum.get("level") or "MEDIUM").upper(), evidence_level="L2",
                )
                events.append(event); decisions.append(decision)

            for index, ev in enumerate(session.get("dtmf_quality_events", []) or []):
                start = session_start + float(ev.get("start_seconds") or 0.0)
                end = session_start + float(ev.get("end_seconds") if ev.get("end_seconds") is not None else ev.get("start_seconds") or 0.0)
                event, decision = _direct_event(
                    analyzer_id="pcm_intelligence", analyzer_states=analyzer_states, event_type="DTMF_ABNORMAL",
                    scope=base_scope, time_range={"start": start, "end": end, "representative": start},
                    measurements=ev, evidence_refs=[],
                    source_ref={"source": "pcm.dtmf_quality_events", "stream_index": stream_index, "session_index": session_index, "index": index},
                    severity=ev.get("severity", "MEDIUM"), evidence_level="L3",
                )
                events.append(event); decisions.append(decision)

    media_events = list(media.get("cross_layer_events", []) or []) + list(media.get("periodic_interference_paths", []) or [])
    for index, raw in enumerate(media_events):
        details = raw.get("details") or {}
        legacy = details.get("candidate_decision") or {}
        candidate_id = str(legacy.get("candidate_id") or "")
        if candidate_id and candidate_id in candidate_event_ids:
            continue
        ftype = str(raw.get("type") or "MEDIA_ANOMALY")
        event, decision = _direct_event(
            analyzer_id="media_intelligence", analyzer_states=analyzer_states, event_type=ftype,
            scope=raw.get("scope") or {}, time_range=_event_time(raw), measurements=details,
            evidence_refs=[], source_ref={"source": "media.cross_layer", "index": index},
            severity=raw.get("severity"), evidence_level=raw.get("evidence_level"),
        )
        events.append(event); decisions.append(decision)

    # Stable de-duplication allows the same promoted media event to be visible in
    # multiple Analyzer collections without creating multiple canonical facts.
    unique_events = {str(x["event_id"]): x for x in events}
    unique_decisions = {str(x["decision_id"]): x for x in decisions}
    status_counts = Counter(str(x["status"]) for x in unique_decisions.values())
    return {
        "schema_version": DIAGNOSTIC_CONTRACT_SNAPSHOT_VERSION,
        "event_schema_version": DIAGNOSTIC_EVENT_SCHEMA_VERSION,
        "candidate_decision_schema_version": CANDIDATE_DECISION_SCHEMA_VERSION,
        "finding_link_schema_version": FINDING_DIAGNOSTIC_LINK_VERSION,
        "events": sorted(unique_events.values(), key=lambda x: x["event_id"]),
        "candidate_decisions": sorted(unique_decisions.values(), key=lambda x: x["decision_id"]),
        "summary": {
            "event_count": len(unique_events),
            "candidate_decision_count": len(unique_decisions),
            "decision_status_counts": dict(sorted(status_counts.items())),
            "finding_fallback_event_count": 0,
            "finding_merge_decision_count": 0,
        },
    }


def _accepted_decision_index(snapshot: dict) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for decision in snapshot.get("candidate_decisions", []) or []:
        if decision.get("status") != CandidateDecisionStatus.ACCEPT.value:
            continue
        out.setdefault(str(decision.get("event_id")), []).append(decision)
    return out


def _candidate_id_event(snapshot: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for event in snapshot.get("events", []) or []:
        candidate_id = str(((event.get("context") or {}).get("legacy_candidate_id")) or "")
        if candidate_id:
            out[candidate_id] = event
    return out


def _fallback_event(finding: dict) -> tuple[dict, dict]:
    event = build_diagnostic_event(
        event_type=str(finding.get("type") or "UNKNOWN_FINDING"),
        analyzer_id="finding_composer_adapter",
        analyzer_version="v1",
        scope=finding.get("scope") or {},
        time_range=finding.get("time_range") or {},
        measurements=finding.get("metrics") or {},
        evidence_refs=finding.get("evidence_refs") or [],
        quality={"severity": finding.get("severity"), "evidence_level": finding.get("evidence_level")},
        source_ref={"source": "finding.adapter", "stable_key": finding.get("stable_key")},
    )
    decision = build_candidate_decision(
        event,
        status=CandidateDecisionStatus.ACCEPT,
        reason_code="LEGACY_FINDING_SOURCE_ADAPTED",
        reason="PR7 无损兼容适配：现有 Finding 尚无可唯一匹配的 Analyzer Event，未提升证据等级。",
        rule_version="finding-source-adapter-v1",
    )
    return event, decision


def attach_finding_diagnostic_links(*, findings: list[dict], snapshot: dict) -> dict:
    events = list(snapshot.get("events", []) or [])
    decisions = list(snapshot.get("candidate_decisions", []) or [])
    accepted_index = _accepted_decision_index(snapshot)
    candidate_events = _candidate_id_event(snapshot)
    fallback_count = 0
    merge_count = 0

    for finding in findings:
        ftype = str(finding.get("type") or "")
        scope = finding.get("scope") or {}
        matched: list[dict] = []
        candidate_id = str((((finding.get("metrics") or {}).get("candidate_decision") or {}).get("candidate_id")) or "")
        if candidate_id and candidate_id in candidate_events:
            event = candidate_events[candidate_id]
            if event.get("event_id") in accepted_index:
                matched = [event]

        if not matched:
            for event in events:
                if str(event.get("event_type") or "") != ftype:
                    continue
                if event.get("event_id") not in accepted_index:
                    continue
                if not _scope_matches(event.get("scope") or {}, scope):
                    continue
                if not _time_in_finding(event, finding):
                    continue
                matched.append(event)

        if not matched:
            event, decision = _fallback_event(finding)
            events.append(event); decisions.append(decision)
            accepted_index.setdefault(event["event_id"], []).append(decision)
            matched = [event]
            fallback_count += 1

        matched.sort(key=lambda x: (x.get("time_range") or {}).get("representative") is None or False,
                                   (x.get("time_range") or {}).get("representative") or 0,
                                   x.get("event_id") or "")
        link_decisions: list[dict] = []
        for event in matched:
            link_decisions.extend(accepted_index.get(str(event["event_id"]), []))

        primary_event_id = matched[0]["event_id"]
        for event in matched[1:]:
            merge = build_candidate_decision(
                event,
                status=CandidateDecisionStatus.MERGE,
                reason_code="SAME_FINDING_SIGNATURE",
                reason="同一 Finding Signature/Scope/时间范围内的有效事件合并为一个稳定 Finding。",
                rule_version=FINDING_MERGE_RULE_VERSION,
                merge_target_event_id=primary_event_id,
                related_event_refs=[{"event_id": primary_event_id}],
            )
            decisions.append(merge)
            link_decisions.append(merge)
            merge_count += 1

        finding["diagnostic"] = build_finding_diagnostic_link(events=matched, decisions=link_decisions)

    unique_events = {str(x["event_id"]): x for x in events}
    unique_decisions = {str(x["decision_id"]): x for x in decisions}
    status_counts = Counter(str(x["status"]) for x in unique_decisions.values())
    snapshot["events"] = sorted(unique_events.values(), key=lambda x: x["event_id"])
    snapshot["candidate_decisions"] = sorted(unique_decisions.values(), key=lambda x: x["decision_id"])
    snapshot["summary"] = {
        "event_count": len(unique_events),
        "candidate_decision_count": len(unique_decisions),
        "decision_status_counts": dict(sorted(status_counts.items())),
        "finding_fallback_event_count": fallback_count,
        "finding_merge_decision_count": merge_count,
    }
    return snapshot


def validate_diagnostic_contract_snapshot(snapshot: dict, *, findings: list[dict]) -> None:
    if snapshot.get("schema_version") != DIAGNOSTIC_CONTRACT_SNAPSHOT_VERSION:
        raise DiagnosticContractError("DIAGNOSTIC_CONTRACT_SNAPSHOT_SCHEMA_UNSUPPORTED")
    event_ids: set[str] = set()
    for event in snapshot.get("events", []) or []:
        validate_diagnostic_event(event)
        event_id = str(event["event_id"])
        if event_id in event_ids:
            raise DiagnosticContractError("DIAGNOSTIC_EVENT_ID_DUPLICATE")
        event_ids.add(event_id)
    decision_ids: set[str] = set()
    for decision in snapshot.get("candidate_decisions", []) or []:
        validate_candidate_decision(decision)
        decision_id = str(decision["decision_id"])
        if decision_id in decision_ids:
            raise DiagnosticContractError("CANDIDATE_DECISION_ID_DUPLICATE")
        decision_ids.add(decision_id)
        if str(decision["event_id"]) not in event_ids:
            raise DiagnosticContractError("CANDIDATE_DECISION_EVENT_NOT_FOUND")
    for finding in findings:
        link = finding.get("diagnostic") or {}
        validate_finding_diagnostic_link(link, require_accepted=True)
        if any(str(event_id) not in event_ids for event_id in link.get("event_ids") or []):
            raise DiagnosticContractError("FINDING_DIAGNOSTIC_EVENT_NOT_IN_SNAPSHOT")
        if any(str(decision_id) not in decision_ids for decision_id in link.get("decision_ids") or []):
            raise DiagnosticContractError("FINDING_DIAGNOSTIC_DECISION_NOT_IN_SNAPSHOT")

    counts = Counter(str(x.get("status") or "") for x in snapshot.get("candidate_decisions", []) or [])
    summary = snapshot.get("summary") or {}
    if int(summary.get("event_count") or -1) != len(event_ids):
        raise DiagnosticContractError("DIAGNOSTIC_SUMMARY_EVENT_COUNT_MISMATCH")
    if int(summary.get("candidate_decision_count") or -1) != len(decision_ids):
        raise DiagnosticContractError("DIAGNOSTIC_SUMMARY_DECISION_COUNT_MISMATCH")
    if dict(sorted(counts.items())) != dict(summary.get("decision_status_counts") or {}):
        raise DiagnosticContractError("DIAGNOSTIC_SUMMARY_STATUS_COUNT_MISMATCH")
