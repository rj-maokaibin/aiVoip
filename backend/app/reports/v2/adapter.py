from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable, Mapping

from .call_reconstruction import reconstruct_call_v2
from .composer import compose_preliminary_report_v2
from .correlation import absorb_member_findings, correlate_media_events
from .finding_events import aggregate_events, build_event
from .timeline import build_timeline_v2, event_relative_time
from .visibility import calculate_visibility


def compose_v2_from_analyzers(
    *,
    report_id: str,
    sip_call: Mapping[str, Any],
    packet: Mapping[str, Any] | None,
    pcm: Mapping[str, Any] | None,
    capture_window: Mapping[str, Any] | None = None,
    signaling_legs: Iterable[Mapping[str, Any]] = (),
    media_legs: Iterable[Mapping[str, Any]] = (),
    dtmf_dial_match: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a V2 report from already-normalized analyzer facts.

    This adapter deliberately does not parse packets, infer SIP dialogs from raw
    text, or guess missing media legs. Analyzer/state-machine outputs remain the
    factual authority; missing mapping stays missing.
    """

    call = reconstruct_call_v2(sip_call)
    packet_dict = dict(packet or {})
    pcm_dict = dict(pcm or {})
    rtp_streams = [dict(item) for item in packet_dict.get("rtp_streams") or []]
    pcm_windows = _pcm_windows(pcm_dict)
    timeline = build_timeline_v2(call, rtp_streams, pcm_windows=pcm_windows, capture_window=capture_window)

    events: list[dict[str, Any]] = []
    events.extend(_packet_events(packet_dict, call_id=call.get("call_id")))
    events.extend(_pcm_events(pcm_dict, call_id=call.get("call_id")))
    _attach_relative_times(events, call=call, timeline=timeline)

    findings = _findings_from_events(events)
    clusters = correlate_media_events(events, threshold_ms=50.0)
    findings = absorb_member_findings(findings, clusters)

    visibility = calculate_visibility(
        signaling_legs=signaling_legs,
        media_legs=media_legs,
        termination=call.get("termination") or {},
    )
    normal_evidence: list[dict[str, Any]] = []
    exclusion_evidence: list[dict[str, Any]] = []
    symptom_assessment: dict[str, Any] = {}
    if dtmf_dial_match:
        match = bool(dtmf_dial_match.get("match"))
        evidence = {
            "type": "DTMF_SIP_DIAL_MATCH" if match else "DTMF_SIP_DIAL_MISMATCH",
            "pcm_digits": dtmf_dial_match.get("pcm_digits"),
            "sip_target": dtmf_dial_match.get("sip_target"),
            "evidence_refs": list(dtmf_dial_match.get("evidence_refs") or []),
        }
        if match:
            normal_evidence.append(evidence)
            symptom_assessment = {
                "reproduced": False,
                "detail": (
                    f"PCM DTMF {dtmf_dial_match.get('pcm_digits')} 与 SIP target "
                    f"{dtmf_dial_match.get('sip_target')} 一致。"
                ),
            }
        else:
            symptom_assessment = {
                "reproduced": True,
                "detail": (
                    f"PCM DTMF {dtmf_dial_match.get('pcm_digits')} 与 SIP target "
                    f"{dtmf_dial_match.get('sip_target')} 不一致。"
                ),
            }

    return compose_preliminary_report_v2(
        report_id=report_id,
        call_reconstruction=call,
        timeline=timeline,
        rtp_streams=rtp_streams,
        events=events,
        findings=findings,
        correlation_clusters=clusters,
        visibility=visibility,
        normal_evidence=normal_evidence,
        exclusion_evidence=exclusion_evidence,
        symptom_assessment=symptom_assessment,
    )


def _packet_events(packet: Mapping[str, Any], *, call_id: str | None) -> list[dict[str, Any]]:
    streams = {str(item.get("stream_id")): item for item in packet.get("rtp_streams") or [] if item.get("stream_id")}
    out: list[dict[str, Any]] = []
    for index, anomaly in enumerate(packet.get("anomalies") or []):
        if not isinstance(anomaly, Mapping):
            continue
        anomaly_type = str(anomaly.get("type") or "").upper()
        if anomaly_type not in {"HIGH_DELTA", "PACKET_LOSS", "BURST_LOSS"}:
            continue
        evidence = anomaly.get("evidence") or {}
        stream = streams.get(str(evidence.get("stream_id") or "")) or {}
        when = _time(anomaly)
        if when is None:
            continue
        observation_type = "RTP_HIGH_DELTA" if anomaly_type == "HIGH_DELTA" else "RTP_SEQUENCE_LOSS"
        layer = _rtp_layer(stream, evidence)
        evidence_ref = f"packet.anomalies[{index}]"
        metrics = dict(evidence)
        metrics.update({
            "lost_packets": stream.get("lost_packets", stream.get("lost")),
            "sequence_continuous": _sequence_continuous(anomaly, stream),
            "max_delta_ms": evidence.get("delta_ms", stream.get("max_delta_ms")),
        })
        out.append(build_event(
            event_id=f"PKT-{index:04d}",
            observation_type=observation_type,
            timestamp=when,
            layer=layer,
            source_ref=evidence_ref,
            call_id=str(evidence.get("call_id") or call_id or "") or None,
            direction=stream.get("direction") or evidence.get("direction"),
            media_path=stream.get("media_path") or stream.get("call_direction_role"),
            metrics=metrics,
            evidence_refs=[evidence_ref],
        ))
    return out


def _pcm_events(pcm: Mapping[str, Any], *, call_id: str | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    event_index = 0
    for stream_index, stream in enumerate(pcm.get("streams") or []):
        if not isinstance(stream, Mapping):
            continue
        tap = stream.get("tap") or {}
        tap_name = str(tap.get("name") or "PCM").upper()
        layer = tap_name if tap_name.startswith("PCM_") else f"PCM_{tap_name}" if tap_name in {"RX", "TX"} else tap_name
        for session_index, session in enumerate(stream.get("sessions") or []):
            if not isinstance(session, Mapping):
                continue
            for gap_index, gap in enumerate(session.get("gap_events") or []):
                if not isinstance(gap, Mapping):
                    continue
                when = _time(gap)
                if when is None:
                    continue
                evidence_ref = f"pcm.streams[{stream_index}].sessions[{session_index}].gap_events[{gap_index}]"
                out.append(build_event(
                    event_id=f"PCM-{event_index:04d}",
                    observation_type="PCM_PACKET_INTERVAL_SPIKE",
                    timestamp=when,
                    layer=layer,
                    source_ref=evidence_ref,
                    call_id=call_id,
                    direction=tap.get("direction"),
                    media_path=tap.get("media_path") or tap.get("path_role"),
                    metrics=dict(gap),
                    evidence_refs=[evidence_ref],
                ))
                event_index += 1
    return out


def _findings_from_events(events: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str | None, str | None], list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        key = (
            str(event.get("observation_type") or "UNKNOWN"),
            str(event.get("layer") or "UNKNOWN"),
            event.get("call_id"),
            event.get("direction"),
        )
        grouped[key].append(dict(event))

    findings: list[dict[str, Any]] = []
    for number, ((observation, layer, _call_id, _direction), members) in enumerate(sorted(grouped.items()), start=1):
        severity = "MEDIUM"
        finding = aggregate_events(
            members,
            finding_id=f"F-{number:03d}",
            finding_type=observation,
            severity=severity,
            finding_class="ABNORMAL",
            title=_finding_title(observation, layer),
        )
        finding["evidence_refs"] = sorted({str(ref) for item in members for ref in item.get("evidence_refs") or []})
        if observation == "RTP_SEQUENCE_LOSS":
            lost_values = [int((item.get("metrics") or {}).get("lost_packets") or 0) for item in members]
            sequence_values = [(item.get("metrics") or {}).get("sequence_continuous") for item in members]
            finding["metrics"] = {
                "lost_packets": max(lost_values, default=0),
                "sequence_continuous": all(value is True for value in sequence_values) if sequence_values else None,
            }
        findings.append(finding)
    return findings


def _pcm_windows(pcm: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    windows: dict[str, dict[str, Any]] = {}
    for stream in pcm.get("streams") or []:
        if not isinstance(stream, Mapping):
            continue
        tap = stream.get("tap") or {}
        name = str(tap.get("name") or "PCM").upper()
        starts: list[float] = []
        ends: list[float] = []
        for session in stream.get("sessions") or []:
            if not isinstance(session, Mapping):
                continue
            if session.get("start_time") is not None:
                starts.append(float(session["start_time"]))
            if session.get("end_time") is not None:
                ends.append(float(session["end_time"]))
        if starts and ends:
            windows[name] = {"start": min(starts), "end": max(ends)}
    return windows


def _attach_relative_times(events: list[dict[str, Any]], *, call: Mapping[str, Any], timeline: Mapping[str, Any]) -> None:
    invite = call.get("invite_time")
    established = call.get("established_time")
    media_start = (timeline.get("media_observation_window") or {}).get("start")
    for event in events:
        when = event.get("timestamp")
        event["relative_to_invite"] = event_relative_time(when, invite)
        event["relative_to_established"] = event_relative_time(when, established)
        event["relative_to_media_start"] = event_relative_time(when, media_start)


def _time(item: Mapping[str, Any]) -> float | None:
    for key in ("representative_time", "time", "start_time", "timestamp"):
        if item.get(key) is not None:
            return float(item[key])
    return None


def _sequence_continuous(anomaly: Mapping[str, Any], stream: Mapping[str, Any]) -> bool | None:
    evidence = anomaly.get("evidence") or {}
    if evidence.get("sequence_continuous") is not None:
        return bool(evidence.get("sequence_continuous"))
    lost = stream.get("lost_packets", stream.get("lost"))
    if lost is not None:
        return int(lost) == 0
    return None


def _rtp_layer(stream: Mapping[str, Any], evidence: Mapping[str, Any]) -> str:
    role = str(stream.get("call_direction_role") or evidence.get("call_direction_role") or "").upper()
    if role in {"CALLER_TO_PBX", "UPSTREAM", "CALLER_UPSTREAM"}:
        return "RTP_UPSTREAM"
    if role in {"PBX_TO_CALLER", "DOWNSTREAM", "CALLER_DOWNSTREAM"}:
        return "RTP_DOWNSTREAM"
    return "RTP"


def _finding_title(observation: str, layer: str) -> str:
    if observation == "PCM_PACKET_INTERVAL_SPIKE":
        return f"{layer} packet timing spike"
    if observation == "RTP_HIGH_DELTA":
        return "RTP packet timing spike"
    if observation == "RTP_SEQUENCE_LOSS":
        return "RTP sequence loss"
    return observation.replace("_", " ").title()
