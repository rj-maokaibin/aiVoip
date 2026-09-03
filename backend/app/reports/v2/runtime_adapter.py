from __future__ import annotations

from collections import defaultdict
from typing import Any, Mapping

from .adapter import (
    _packet_events,
    _pcm_events,
    _pcm_windows,
    build_logical_call_context,
)
from .call_reconstruction import reconstruct_call_v2
from .composer import compose_preliminary_report_v2
from .correlation import absorb_member_findings, correlate_media_events
from .finding_events import aggregate_events
from .phase import classify_event_phase, finding_class_for_phase
from .timeline import build_timeline_v2, event_relative_time
from .visibility import calculate_visibility


def compose_v2_runtime_from_analyzers(
    *,
    report_id: str,
    sip_call: Mapping[str, Any],
    packet: Mapping[str, Any] | None,
    pcm: Mapping[str, Any] | None,
    media: Mapping[str, Any] | None = None,
    subject_device_ip: str | None = None,
    capture_window: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Production adapter from authoritative analyzer output to canonical V2.

    Unlike the foundation adapter, this path additionally classifies event phase
    before Finding aggregation. A pre-call PCM cadence spike remains reviewable
    evidence but is not counted as an in-call media failure.
    """

    packet_dict = dict(packet or {})
    pcm_dict = dict(pcm or {})
    call = reconstruct_call_v2(sip_call)
    logical = build_logical_call_context(
        packet_dict,
        selected_call=sip_call,
        subject_device_ip=subject_device_ip,
    )
    call["logical_call"] = logical["logical_call"]

    rtp_streams = [dict(item) for item in packet_dict.get("rtp_streams") or []]
    timeline = build_timeline_v2(
        call,
        rtp_streams,
        pcm_windows=_pcm_windows(pcm_dict),
        capture_window=capture_window or derive_capture_window(packet_dict),
    )

    events: list[dict[str, Any]] = []
    events.extend(
        _packet_events(
            packet_dict,
            call_id=call.get("call_id"),
            call_id_map=logical["call_id_map"],
            stream_layers=logical["stream_layers"],
        )
    )
    events.extend(_pcm_events(pcm_dict, call_id=call.get("call_id")))
    _annotate_event_context(events, call=call, timeline=timeline)

    findings = _phase_aware_findings(events)
    clusters = correlate_media_events(
        [event for event in events if event.get("phase") == "ACTIVE_MEDIA"],
        threshold_ms=50.0,
    )
    findings = absorb_member_findings(findings, clusters)

    visibility = calculate_visibility(
        signaling_legs=logical["signaling_legs"],
        media_legs=logical["media_legs"],
        termination=call.get("termination") or {},
    )
    visibility["logical_call_mapping"] = (
        "AMBIGUOUS"
        if logical["logical_call"].get("ambiguous_leg_mapping")
        else "RESOLVED"
        if logical["logical_call"].get("sip_leg_count")
        else "UNAVAILABLE"
    )
    if visibility["logical_call_mapping"] == "AMBIGUOUS":
        visibility["root_cause_readiness"] = "INSUFFICIENT"

    dtmf = extract_dtmf_match_from_media(
        media,
        canonical_call_id=str(call.get("call_id") or "") or None,
        call_id_map=logical["call_id_map"],
    )
    normal_evidence: list[dict[str, Any]] = []
    symptom_assessment: dict[str, Any] = {}
    if dtmf:
        normal_evidence.append(
            {
                "type": "DTMF_SIP_DIAL_MATCH",
                "pcm_digits": dtmf.get("pcm_digits"),
                "sip_target": dtmf.get("sip_target"),
                "evidence_refs": dtmf.get("evidence_refs") or [],
            }
        )
        symptom_assessment = {
            "reproduced": False,
            "detail": (
                f"PCM DTMF {dtmf.get('pcm_digits')} 与 SIP target "
                f"{dtmf.get('sip_target')} 一致；本次证据未复现拨号号码丢失。"
            ),
        }

    report = compose_preliminary_report_v2(
        report_id=report_id,
        call_reconstruction=call,
        timeline=timeline,
        rtp_streams=rtp_streams,
        events=events,
        findings=findings,
        correlation_clusters=clusters,
        visibility=visibility,
        normal_evidence=normal_evidence,
        symptom_assessment=symptom_assessment,
    )
    report["logical_call"] = logical["logical_call"]
    report["phase_summary"] = _phase_summary(events)
    return report


def derive_capture_window(packet: Mapping[str, Any]) -> dict[str, float | None]:
    """Prefer explicit capture facts; otherwise use observed SIP/RTP boundaries."""

    summary = packet.get("summary") or {}
    candidates_start = [
        summary.get("capture_start_time"),
        packet.get("capture_start_time"),
    ]
    candidates_end = [
        summary.get("capture_end_time"),
        packet.get("capture_end_time"),
    ]
    for call in packet.get("calls") or []:
        if isinstance(call, Mapping):
            candidates_start.append(call.get("start_time"))
            candidates_end.append(call.get("end_time"))
            for item in call.get("ladder") or []:
                if isinstance(item, Mapping):
                    candidates_start.append(item.get("timestamp"))
                    candidates_end.append(item.get("timestamp"))
    for stream in packet.get("rtp_streams") or []:
        if isinstance(stream, Mapping):
            candidates_start.append(stream.get("start_time"))
            candidates_end.append(stream.get("end_time"))
    starts = [_number(value) for value in candidates_start]
    ends = [_number(value) for value in candidates_end]
    starts = [value for value in starts if value is not None]
    ends = [value for value in ends if value is not None]
    return {
        "start": min(starts) if starts else None,
        "end": max(ends) if ends else None,
    }


def extract_dtmf_match_from_media(
    media: Mapping[str, Any] | None,
    *,
    canonical_call_id: str | None,
    call_id_map: Mapping[str, str] | None = None,
) -> dict[str, Any] | None:
    """Return only explicit DTMF/SIP matches; absence never means mismatch."""

    call_id_map = call_id_map or {}
    for index, event in enumerate((media or {}).get("cross_layer_events") or []):
        if not isinstance(event, Mapping):
            continue
        if str(event.get("type") or "").upper() != "DTMF_SIP_DIAL_MATCH":
            continue
        scope = event.get("scope") or {}
        raw_call_id = str(scope.get("call_id") or "")
        mapped = call_id_map.get(raw_call_id) or raw_call_id or canonical_call_id
        if canonical_call_id and mapped and mapped != canonical_call_id:
            continue
        details = event.get("details") or {}
        if details.get("match") is not True:
            continue
        return {
            "match": True,
            "pcm_digits": details.get("pcm_digits"),
            "sip_target": details.get("sip_target"),
            "timestamp": event.get("time"),
            "evidence_refs": [f"media.cross_layer_events[{index}]"],
        }
    return None


def _annotate_event_context(
    events: list[dict[str, Any]],
    *,
    call: Mapping[str, Any],
    timeline: Mapping[str, Any],
) -> None:
    invite = _number(call.get("invite_time"))
    established = _number(call.get("established_time"))
    media = timeline.get("media_observation_window") or {}
    media_start = _number(media.get("start")) if isinstance(media, Mapping) else None
    for event in events:
        when = _number(event.get("timestamp"))
        event["phase"] = classify_event_phase(when, call=call, timeline=timeline)
        event["relative_to_invite"] = event_relative_time(when, invite)
        event["relative_to_established"] = event_relative_time(when, established)
        event["relative_to_media_start"] = event_relative_time(when, media_start)


def _phase_aware_findings(events: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str | None, str | None, str], list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        item = dict(event)
        key = (
            str(item.get("observation_type") or "UNKNOWN"),
            str(item.get("layer") or "UNKNOWN"),
            item.get("call_id"),
            item.get("direction"),
            str(item.get("phase") or "UNSCOPED"),
        )
        grouped[key].append(item)

    findings: list[dict[str, Any]] = []
    for number, (key, members) in enumerate(sorted(grouped.items(), key=lambda pair: tuple(str(x) for x in pair[0])), start=1):
        observation, layer, _call_id, _direction, phase = key
        finding_class = finding_class_for_phase(observation, phase)
        finding = aggregate_events(
            members,
            finding_id=f"F-{number:03d}",
            finding_type=observation,
            severity="MEDIUM",
            finding_class=finding_class,
            title=_finding_title(observation, layer, phase),
        )
        finding["phase"] = phase
        finding["evidence_refs"] = sorted(
            {str(ref) for item in members for ref in item.get("evidence_refs") or []}
        )
        if observation == "RTP_SEQUENCE_LOSS":
            lost_values = [int((item.get("metrics") or {}).get("lost_packets") or 0) for item in members]
            continuous_values = [(item.get("metrics") or {}).get("sequence_continuous") for item in members]
            finding["metrics"] = {
                "lost_packets": max(lost_values, default=0),
                "sequence_continuous": all(value is True for value in continuous_values) if continuous_values else None,
            }
        findings.append(finding)
    return findings


def _finding_title(observation: str, layer: str, phase: str) -> str:
    phase_suffix = "" if phase == "ACTIVE_MEDIA" else f" [{phase}]"
    if observation == "PCM_PACKET_INTERVAL_SPIKE":
        return f"{layer} packet timing spike{phase_suffix}"
    if observation == "RTP_HIGH_DELTA":
        return f"RTP packet timing spike{phase_suffix}"
    if observation == "RTP_SEQUENCE_LOSS":
        return f"RTP sequence loss{phase_suffix}"
    return observation.replace("_", " ").title() + phase_suffix


def _phase_summary(events: list[Mapping[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for event in events:
        counts[str(event.get("phase") or "UNSCOPED")] += 1
    return dict(sorted(counts.items()))


def _number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
