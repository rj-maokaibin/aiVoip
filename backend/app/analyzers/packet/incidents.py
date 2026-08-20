from __future__ import annotations

import hashlib
import json
from typing import Any


RTP_INCIDENT_TYPES = {"PACKET_LOSS", "BURST_LOSS", "HIGH_DELTA", "PAYLOAD_CHANGE"}


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _incident_id(material: dict) -> str:
    return "rtpi-" + hashlib.sha256(_canonical(material).encode("utf-8")).hexdigest()[:24]


def _call_for_stream(calls: list[dict], stream_id: str) -> dict | None:
    matches = [c for c in calls if stream_id in (c.get("rtp_stream_ids") or [])]
    if not matches:
        return None
    matches.sort(key=lambda c: (c.get("media_start_time") is None, c.get("media_start_time") or c.get("start_time") or 0.0))
    return matches[0]


def _endpoint_tuple(value: dict | None) -> tuple[str | None, int | None]:
    value = value or {}
    port = value.get("port")
    try:
        port = int(port) if port is not None else None
    except (TypeError, ValueError):
        port = None
    return value.get("ip"), port


def _media_role(call: dict | None, stream: dict) -> str:
    if not call:
        return "UNKNOWN"
    health = call.get("media_direction_health") or {}
    a = _endpoint_tuple(health.get("endpoint_a"))
    b = _endpoint_tuple(health.get("endpoint_b"))
    src = (stream.get("src_ip"), stream.get("src_port"))
    dst = (stream.get("dst_ip"), stream.get("dst_port"))
    if src == a and dst == b:
        return "OFFERER_TO_ANSWERER"
    if src == b and dst == a:
        return "ANSWERER_TO_OFFERER"
    return "CALL_SCOPED_OTHER_LEG" if stream.get("stream_id") in (call.get("rtp_stream_ids") or []) else "UNKNOWN"


def _sequence_boundary(details: dict) -> dict:
    previous = details.get("previous_sequence")
    current = details.get("current_sequence")
    if previous is None or current is None:
        return {"available": False, "sequence_contiguous": None, "sequence_step": None, "packet_loss_at_boundary": None}
    try:
        prev = int(previous) & 0xFFFF
        cur = int(current) & 0xFFFF
    except (TypeError, ValueError):
        return {"available": False, "sequence_contiguous": None, "sequence_step": None, "packet_loss_at_boundary": None}
    step = (cur - prev) & 0xFFFF
    # Forward sequence progress is unambiguous only for the near half of the sequence space.
    if step == 0 or step >= 0x8000:
        return {"available": True, "sequence_contiguous": False, "sequence_step": step, "packet_loss_at_boundary": None, "note": "duplicate/reordered-or-ambiguous-sequence-boundary"}
    return {
        "available": True,
        "sequence_contiguous": step == 1,
        "sequence_step": step,
        "packet_loss_at_boundary": max(0, step - 1),
    }


def _call_relative_time(call: dict | None, event_time: float | None) -> float | None:
    if not call or event_time is None:
        return None
    base = call.get("media_start_time")
    if base is None:
        base = call.get("start_time")
    if base is None:
        return None
    try:
        return round(float(event_time) - float(base), 6)
    except (TypeError, ValueError):
        return None


def _packet_refs(event_type: str, details: dict) -> list[dict]:
    refs = []
    pairs = []
    if event_type == "HIGH_DELTA":
        pairs = [
            ("previous", details.get("previous_frame_number"), details.get("previous_timestamp"), details.get("previous_sequence")),
            ("current", details.get("current_frame_number"), details.get("current_timestamp"), details.get("current_sequence")),
        ]
    elif event_type in {"PACKET_LOSS", "BURST_LOSS"}:
        pairs = [
            ("previous_boundary", details.get("previous_frame_number"), details.get("previous_timestamp"), details.get("previous_sequence_ext")),
            ("next_boundary", details.get("next_frame_number"), details.get("next_timestamp"), details.get("next_sequence_ext")),
        ]
    for role, frame, timestamp, sequence in pairs:
        if frame is None:
            continue
        refs.append({"role": role, "frame_number": frame, "timestamp": timestamp, "sequence": sequence})
    return refs


def _semantics(event_type: str, sequence: dict, details: dict) -> tuple[str, str]:
    if event_type == "HIGH_DELTA":
        if sequence.get("sequence_contiguous") is True:
            return (
                "RTP_CADENCE_STALL_WITHOUT_SEQUENCE_GAP",
                "包间隔显著增大，但该事件边界前后 RTP Sequence 连续；这是发送/到达节奏停顿证据，不等同于 RTP 丢包。",
            )
        if isinstance(sequence.get("packet_loss_at_boundary"), int) and sequence.get("packet_loss_at_boundary", 0) > 0:
            return (
                "RTP_CADENCE_STALL_WITH_SEQUENCE_GAP",
                "包间隔显著增大且事件边界存在 RTP Sequence 缺口；需要同时评估节奏停顿与丢包。",
            )
        return (
            "RTP_CADENCE_STALL_SEQUENCE_UNCERTAIN",
            "包间隔显著增大，但当前 Sequence 边界不足以独立判断是否同时发生丢包。",
        )
    if event_type in {"PACKET_LOSS", "BURST_LOSS"}:
        lost = details.get("lost_packets")
        return ("RTP_SEQUENCE_LOSS_BOUNDARY", f"RTP Sequence 边界确认存在 {lost} 个缺失包；边界 Frame 是可复核证据。")
    if event_type == "PAYLOAD_CHANGE":
        return ("RTP_PAYLOAD_TRANSITION", "RTP Payload Type 在流内发生变化，需要结合 SDP/Codec 上下文解释。")
    return ("RTP_INCIDENT", "RTP 流中存在可复核异常事件。")


def build_rtp_incidents(calls: list[dict] | None, streams: list[dict] | None) -> list[dict]:
    calls = list(calls or [])
    incidents: list[dict] = []
    for stream in streams or []:
        stream_id = str(stream.get("stream_id") or "")
        call = _call_for_stream(calls, stream_id)
        direction = {
            "src_ip": stream.get("src_ip"),
            "src_port": stream.get("src_port"),
            "dst_ip": stream.get("dst_ip"),
            "dst_port": stream.get("dst_port"),
            "text": f"{stream.get('src_ip')}:{stream.get('src_port')}->{stream.get('dst_ip')}:{stream.get('dst_port')}",
        }
        for index, event in enumerate(stream.get("events", []) or []):
            event_type = str(event.get("type") or "")
            if event_type not in RTP_INCIDENT_TYPES:
                continue
            details = dict(event.get("details") or {})
            try:
                event_time = float(event.get("start_time"))
            except (TypeError, ValueError):
                event_time = None
            sequence = _sequence_boundary(details) if event_type == "HIGH_DELTA" else {
                "available": True if event_type in {"PACKET_LOSS", "BURST_LOSS"} else False,
                "sequence_contiguous": False if event_type in {"PACKET_LOSS", "BURST_LOSS"} else None,
                "packet_loss_at_boundary": details.get("lost_packets") if event_type in {"PACKET_LOSS", "BURST_LOSS"} else None,
            }
            semantic_code, semantic_text = _semantics(event_type, sequence, details)
            incident = {
                "incident_id": _incident_id({
                    "stream_id": stream_id,
                    "type": event_type,
                    "time": event_time,
                    "previous_frame": details.get("previous_frame_number"),
                    "current_frame": details.get("current_frame_number") or details.get("next_frame_number"),
                }),
                "type": event_type,
                "severity": event.get("severity") or "INFO",
                "event_index": index,
                "time_range": {"start": event_time, "end": event_time, "representative": event_time},
                "call_id": call.get("call_id") if call else None,
                "call_relative_time_seconds": _call_relative_time(call, event_time),
                "stream_id": stream_id,
                "ssrc": stream.get("ssrc"),
                "direction": direction,
                "media_role": _media_role(call, stream),
                "codec": stream.get("codec"),
                "ptime_ms": stream.get("ptime_ms"),
                "measurements": {
                    "delta_ms": details.get("delta_ms"),
                    "expected_ptime_ms": details.get("expected_ptime_ms", stream.get("ptime_ms")),
                    "excess_delay_ms": details.get("excess_delay_ms"),
                    "stream_packet_count": stream.get("packet_count"),
                    "stream_lost_packets": stream.get("lost_packets", stream.get("lost")),
                    "stream_loss_rate": stream.get("loss_rate"),
                    "stream_p95_jitter_ms": stream.get("p95_rfc3550_jitter_ms", stream.get("p95_jitter_ms")),
                    "stream_max_delta_ms": stream.get("max_delta_ms"),
                },
                "sequence_boundary": sequence,
                "packet_refs": _packet_refs(event_type, details),
                "semantic_code": semantic_code,
                "semantic_text": semantic_text,
                "raw_event_details": details,
            }
            incidents.append(incident)
    incidents.sort(key=lambda x: (x.get("time_range", {}).get("representative") is None, x.get("time_range", {}).get("representative") or 0.0, x["incident_id"]))
    return incidents


def incident_summary(incidents: list[dict]) -> dict:
    by_type: dict[str, int] = {}
    cadence_without_loss = 0
    for incident in incidents:
        ftype = str(incident.get("type") or "UNKNOWN")
        by_type[ftype] = by_type.get(ftype, 0) + 1
        if incident.get("semantic_code") == "RTP_CADENCE_STALL_WITHOUT_SEQUENCE_GAP":
            cadence_without_loss += 1
    return {
        "count": len(incidents),
        "by_type": by_type,
        "cadence_stall_without_sequence_gap_count": cadence_without_loss,
    }


def enrich_packet_anomalies(anomalies: list[dict] | None, incidents: list[dict]) -> list[dict]:
    """Attach incident semantics to existing Packet Analyzer anomaly records.

    Existing anomaly shape remains backward compatible. Matching is deterministic
    on event type + stream_id + event time, with a narrow numeric tolerance only
    for serialized float roundoff.
    """
    lookup: dict[tuple[str, str, int], dict] = {}
    for incident in incidents:
        t = (incident.get("time_range") or {}).get("representative")
        key = (str(incident.get("type")), str(incident.get("stream_id")), round(float(t or 0.0) * 1_000_000))
        lookup[key] = incident
    out = []
    for anomaly in anomalies or []:
        row = dict(anomaly)
        evidence = dict(row.get("evidence") or {})
        stream_id = evidence.get("stream_id")
        if stream_id:
            try:
                micros = round(float(row.get("time") or row.get("start_time") or 0.0) * 1_000_000)
            except (TypeError, ValueError):
                micros = 0
            incident = lookup.get((str(row.get("type")), str(stream_id), micros))
            if incident:
                evidence.update({
                    "incident_id": incident.get("incident_id"),
                    "call_id": incident.get("call_id"),
                    "call_relative_time_seconds": incident.get("call_relative_time_seconds"),
                    "media_role": incident.get("media_role"),
                    "direction": incident.get("direction"),
                    "incident_semantic_code": incident.get("semantic_code"),
                    "incident_semantic_text": incident.get("semantic_text"),
                    "sequence_boundary": incident.get("sequence_boundary"),
                    "packet_refs": incident.get("packet_refs"),
                    "stream_packet_count": (incident.get("measurements") or {}).get("stream_packet_count"),
                    "stream_lost_packets": (incident.get("measurements") or {}).get("stream_lost_packets"),
                    "stream_loss_rate": (incident.get("measurements") or {}).get("stream_loss_rate"),
                    "stream_p95_jitter_ms": (incident.get("measurements") or {}).get("stream_p95_jitter_ms"),
                })
        row["evidence"] = evidence
        out.append(row)
    return out
