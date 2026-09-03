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
    signaling_legs: Iterable[Mapping[str, Any]] | None = None,
    media_legs: Iterable[Mapping[str, Any]] | None = None,
    dtmf_dial_match: Mapping[str, Any] | None = None,
    subject_device_ip: str | None = None,
) -> dict[str, Any]:
    """Build a V2 report from already-normalized analyzer facts.

    This adapter deliberately does not parse raw packets. It consumes the packet
    and PCM analyzer's normalized facts, reconstructs protocol lifecycle semantics,
    and deterministically folds B2BUA SIP legs that belong to the same diagnostic
    call into one logical call scope. Missing or ambiguous mapping stays partial.
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
    pcm_windows = _pcm_windows(pcm_dict)
    timeline = build_timeline_v2(call, rtp_streams, pcm_windows=pcm_windows, capture_window=capture_window)

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
    _attach_relative_times(events, call=call, timeline=timeline)

    findings = _findings_from_events(events)
    clusters = correlate_media_events(events, threshold_ms=50.0)
    findings = absorb_member_findings(findings, clusters)

    effective_signaling_legs = list(signaling_legs) if signaling_legs is not None else logical["signaling_legs"]
    effective_media_legs = list(media_legs) if media_legs is not None else logical["media_legs"]
    visibility = calculate_visibility(
        signaling_legs=effective_signaling_legs,
        media_legs=effective_media_legs,
        termination=call.get("termination") or {},
    )
    if logical["logical_call"].get("ambiguous_leg_mapping"):
        visibility["root_cause_readiness"] = "INSUFFICIENT"
        visibility["logical_call_mapping"] = "AMBIGUOUS"
    else:
        visibility["logical_call_mapping"] = "RESOLVED" if logical["logical_call"].get("sip_leg_count") else "UNAVAILABLE"

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
        exclusion_evidence=exclusion_evidence,
        symptom_assessment=symptom_assessment,
    )
    report["logical_call"] = logical["logical_call"]
    return report


def build_logical_call_context(
    packet: Mapping[str, Any],
    *,
    selected_call: Mapping[str, Any],
    subject_device_ip: str | None = None,
    overlap_seconds: float = 10.0,
) -> dict[str, Any]:
    """Fold deterministic B2BUA legs into one logical diagnostic call.

    A related leg must share the same dialed user, be temporally close, and share
    at least one signaling hop with the selected DUT-facing leg. Multiple matching
    peer legs are treated as ambiguous/forked and never upgraded to complete E2E
    visibility by unioning their media directions.
    """

    selected_id = str(selected_call.get("call_id") or "")
    calls = [dict(item) for item in packet.get("calls") or [] if isinstance(item, Mapping)]
    selected_raw = next((item for item in calls if str(item.get("call_id") or "") == selected_id), dict(selected_call))
    selected_target = _sip_user(selected_raw.get("callee"))
    selected_start = _number(selected_raw.get("start_time"))
    selected_ips = _signaling_ips(selected_raw)
    subject_ip = str(subject_device_ip or "") or _subject_ip_from_selected_call(selected_raw)
    shared_hop_ips = set(selected_ips)
    if subject_ip:
        shared_hop_ips.discard(subject_ip)

    peers: list[dict[str, Any]] = []
    for candidate in calls:
        candidate_id = str(candidate.get("call_id") or "")
        if not candidate_id or candidate_id == selected_id:
            continue
        target = _sip_user(candidate.get("callee"))
        if selected_target and target and target != selected_target:
            continue
        candidate_start = _number(candidate.get("start_time"))
        if selected_start is not None and candidate_start is not None and abs(candidate_start - selected_start) > float(overlap_seconds):
            continue
        candidate_ips = _signaling_ips(candidate)
        if shared_hop_ips and not (shared_hop_ips & candidate_ips):
            continue
        peers.append(candidate)

    ambiguous = len(peers) > 1
    related_calls = [selected_raw] + (peers if not ambiguous else [])
    call_id_map = {str(item.get("call_id")): selected_id for item in related_calls if item.get("call_id") and selected_id}

    selected_role = _selected_leg_role(selected_raw, subject_ip)
    leg_records: list[dict[str, Any]] = []
    signaling_legs: list[dict[str, Any]] = []
    media_legs: list[dict[str, Any]] = []
    stream_layers: dict[str, str] = {}

    for index, leg in enumerate(related_calls):
        role = selected_role if index == 0 else ("CALLEE" if selected_role == "CALLER" else "CALLER")
        endpoint_ip = subject_ip if index == 0 and subject_ip else _peer_endpoint_ip(leg, shared_hop_ips)
        observed = _signaling_observed(leg)
        directions = _media_directions_for_leg(packet, leg, endpoint_ip)
        signaling_legs.append({"role": role, "observed": observed})
        media_legs.append({"role": role, "directions": directions})
        leg_records.append(
            {
                "role": role,
                "call_id": leg.get("call_id"),
                "caller": _sip_user(leg.get("caller")),
                "callee": _sip_user(leg.get("callee")),
                "state": leg.get("state"),
                "endpoint_ip": endpoint_ip,
                "signaling_observed": observed,
                "media_directions": directions,
                "capture_completeness": leg.get("capture_completeness") or {},
            }
        )
        for stream in _streams_for_call(packet, leg):
            stream_id = str(stream.get("stream_id") or "")
            if not stream_id:
                continue
            direction = _direction_for_endpoint(stream, endpoint_ip)
            if role == "CALLER" and direction == "UPSTREAM":
                stream_layers[stream_id] = "RTP_UPSTREAM"
            elif role == "CALLER" and direction == "DOWNSTREAM":
                stream_layers[stream_id] = "RTP_DOWNSTREAM"
            else:
                stream_layers[stream_id] = "RTP"

    if ambiguous:
        for peer in peers:
            leg_records.append(
                {
                    "role": "AMBIGUOUS_PEER",
                    "call_id": peer.get("call_id"),
                    "caller": _sip_user(peer.get("caller")),
                    "callee": _sip_user(peer.get("callee")),
                    "state": peer.get("state"),
                    "endpoint_ip": None,
                    "signaling_observed": _signaling_observed(peer),
                    "media_directions": [],
                    "capture_completeness": peer.get("capture_completeness") or {},
                }
            )

    logical_call = {
        "logical_call_count": 1 if selected_id else 0,
        "canonical_call_id": selected_id or None,
        "sip_leg_count": len(leg_records),
        "resolved_sip_leg_count": len(related_calls),
        "ambiguous_leg_mapping": ambiguous,
        "caller": _sip_user(selected_raw.get("caller")),
        "callee": selected_target,
        "sip_legs": leg_records,
    }
    return {
        "logical_call": logical_call,
        "call_id_map": call_id_map,
        "stream_layers": stream_layers,
        "signaling_legs": signaling_legs,
        "media_legs": media_legs,
    }


def _packet_events(
    packet: Mapping[str, Any],
    *,
    call_id: str | None,
    call_id_map: Mapping[str, str] | None = None,
    stream_layers: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    streams = {str(item.get("stream_id")): item for item in packet.get("rtp_streams") or [] if item.get("stream_id")}
    call_id_map = call_id_map or {}
    stream_layers = stream_layers or {}
    out: list[dict[str, Any]] = []
    for index, anomaly in enumerate(packet.get("anomalies") or []):
        if not isinstance(anomaly, Mapping):
            continue
        anomaly_type = str(anomaly.get("type") or "").upper()
        if anomaly_type not in {"HIGH_DELTA", "PACKET_LOSS", "BURST_LOSS"}:
            continue
        evidence = anomaly.get("evidence") or {}
        stream_id = str(evidence.get("stream_id") or "")
        stream = streams.get(stream_id) or {}
        when = _time(anomaly)
        if when is None:
            continue
        observation_type = "RTP_HIGH_DELTA" if anomaly_type == "HIGH_DELTA" else "RTP_SEQUENCE_LOSS"
        layer = stream_layers.get(stream_id) or _rtp_layer(stream, evidence)
        evidence_ref = f"packet.anomalies[{index}]"
        metrics = dict(evidence)
        metrics.update({
            "lost_packets": stream.get("lost_packets", stream.get("lost")),
            "sequence_continuous": _sequence_continuous(anomaly, stream),
            "max_delta_ms": evidence.get("delta_ms", stream.get("max_delta_ms")),
        })
        raw_call_id = str(evidence.get("call_id") or stream.get("primary_call_id") or "")
        canonical_call_id = call_id_map.get(raw_call_id) or raw_call_id or call_id
        out.append(build_event(
            event_id=f"PKT-{index:04d}",
            observation_type=observation_type,
            timestamp=when,
            layer=layer,
            source_ref=evidence_ref,
            call_id=canonical_call_id,
            direction=stream.get("direction") or evidence.get("direction"),
            media_path="LOGICAL_CALL_MEDIA" if canonical_call_id and canonical_call_id == call_id else None,
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
                    media_path="LOGICAL_CALL_MEDIA" if call_id else None,
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


def _streams_for_call(packet: Mapping[str, Any], call: Mapping[str, Any]) -> list[dict[str, Any]]:
    ids = {str(value) for value in call.get("rtp_stream_ids") or []}
    call_id = str(call.get("call_id") or "")
    out: list[dict[str, Any]] = []
    for stream in packet.get("rtp_streams") or []:
        if not isinstance(stream, Mapping):
            continue
        stream_id = str(stream.get("stream_id") or "")
        bindings = stream.get("call_bindings") or []
        bound = any(isinstance(item, Mapping) and str(item.get("call_id") or "") == call_id for item in bindings)
        if stream_id in ids or bound:
            out.append(dict(stream))
    return out


def _media_directions_for_leg(packet: Mapping[str, Any], call: Mapping[str, Any], endpoint_ip: str | None) -> list[str]:
    if not endpoint_ip:
        return []
    directions = {
        direction
        for stream in _streams_for_call(packet, call)
        if (direction := _direction_for_endpoint(stream, endpoint_ip))
    }
    return sorted(directions)


def _direction_for_endpoint(stream: Mapping[str, Any], endpoint_ip: str | None) -> str | None:
    if not endpoint_ip:
        return None
    if str(stream.get("src_ip") or "") == endpoint_ip:
        return "UPSTREAM"
    if str(stream.get("dst_ip") or "") == endpoint_ip:
        return "DOWNSTREAM"
    return None


def _signaling_observed(call: Mapping[str, Any]) -> list[str]:
    observed: set[str] = set()
    for item in call.get("ladder") or []:
        if not isinstance(item, Mapping):
            continue
        method = str(item.get("method") or "").upper()
        cseq_method = str(item.get("cseq_method") or "").upper()
        status = item.get("status_code")
        if method == "INVITE":
            observed.add("INVITE")
        elif method == "ACK":
            observed.add("ACK")
        elif status is not None and cseq_method == "INVITE" and int(status) >= 200:
            observed.add("FINAL_RESPONSE")
    return sorted(observed)


def _selected_leg_role(call: Mapping[str, Any], subject_ip: str | None) -> str:
    invite = next((item for item in call.get("ladder") or [] if isinstance(item, Mapping) and str(item.get("method") or "").upper() == "INVITE"), None)
    if invite and subject_ip:
        if _endpoint_ip(invite.get("src")) == subject_ip:
            return "CALLER"
        if _endpoint_ip(invite.get("dst")) == subject_ip:
            return "CALLEE"
    sdp = call.get("sdp") or {}
    offer_ips = _sdp_ips(sdp.get("offer") or {})
    answer_ips = _sdp_ips(sdp.get("answer") or {})
    if subject_ip and subject_ip in offer_ips:
        return "CALLER"
    if subject_ip and subject_ip in answer_ips:
        return "CALLEE"
    return "CALLER"


def _subject_ip_from_selected_call(call: Mapping[str, Any]) -> str | None:
    invite = next((item for item in call.get("ladder") or [] if isinstance(item, Mapping) and str(item.get("method") or "").upper() == "INVITE"), None)
    return _endpoint_ip(invite.get("src")) if invite else None


def _peer_endpoint_ip(call: Mapping[str, Any], shared_hop_ips: set[str]) -> str | None:
    invite = next((item for item in call.get("ladder") or [] if isinstance(item, Mapping) and str(item.get("method") or "").upper() == "INVITE"), None)
    if invite:
        src = _endpoint_ip(invite.get("src"))
        dst = _endpoint_ip(invite.get("dst"))
        if dst and dst not in shared_hop_ips:
            return dst
        if src and src not in shared_hop_ips:
            return src
    for ip in sorted(_signaling_ips(call)):
        if ip not in shared_hop_ips:
            return ip
    return None


def _signaling_ips(call: Mapping[str, Any]) -> set[str]:
    out: set[str] = set()
    for item in call.get("ladder") or []:
        if not isinstance(item, Mapping):
            continue
        for key in ("src", "dst"):
            ip = _endpoint_ip(item.get(key))
            if ip:
                out.add(ip)
    return out


def _sdp_ips(payload: Mapping[str, Any]) -> set[str]:
    out: set[str] = set()
    if payload.get("connection_address"):
        out.add(str(payload["connection_address"]))
    for media in payload.get("media") or []:
        if isinstance(media, Mapping) and media.get("connection_address"):
            out.add(str(media["connection_address"]))
    return out


def _endpoint_ip(value: Any) -> str | None:
    raw = str(value or "")
    if not raw:
        return None
    if raw.count(":") == 1:
        return raw.rsplit(":", 1)[0] or None
    return raw


def _sip_user(uri: Any) -> str | None:
    raw = str(uri or "").strip().strip("<>")
    if not raw:
        return None
    lower = raw.lower()
    pos = lower.find("sips:")
    prefix = 5
    if pos < 0:
        pos = lower.find("sip:")
        prefix = 4
    if pos >= 0:
        raw = raw[pos + prefix:]
    for token in ("@", ";", ">", "?"):
        if token in raw:
            raw = raw.split(token, 1)[0]
    raw = raw.strip()
    return raw or None


def _time(item: Mapping[str, Any]) -> float | None:
    for key in ("representative_time", "time", "start_time", "timestamp"):
        if item.get(key) is not None:
            return float(item[key])
    return None


def _number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
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
