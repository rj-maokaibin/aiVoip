from __future__ import annotations

import re

from app.analyzers.media.subject_identity import SUBJECT_IDENTITY_UNIQUE, infer_pcm_source_device_identity

_URI_DIGITS = re.compile(r"(?:sip:|tel:)?([0-9*#]+)")


def _call_connection_ips(call: dict, packet_result: dict) -> set[str]:
    ips: set[str] = set()
    sdp = call.get("sdp") or {}
    for side in ("offer", "answer"):
        payload = sdp.get(side) or {}
        if payload.get("connection_address"):
            ips.add(str(payload["connection_address"]))
        for media in payload.get("media", []) or []:
            if media.get("connection_address"):
                ips.add(str(media["connection_address"]))
    stream_ids = set(call.get("rtp_stream_ids") or [])
    for stream in packet_result.get("rtp_streams", []) or []:
        if stream.get("stream_id") not in stream_ids:
            continue
        if stream.get("src_ip"):
            ips.add(str(stream["src_ip"]))
        if stream.get("dst_ip"):
            ips.add(str(stream["dst_ip"]))
    return ips


def _subject_calls(packet_result: dict, pcm_result: dict) -> tuple[list[dict], dict]:
    calls = [c for c in packet_result.get("calls", []) or [] if c.get("call_id")]
    identity = infer_pcm_source_device_identity(pcm_result, source="pcm_intelligence")
    if len(calls) <= 1:
        return calls, {"status": "SINGLE_CALL", "subject_identity": identity}

    if identity.get("status") != SUBJECT_IDENTITY_UNIQUE or not identity.get("selected_ip"):
        # Multi-leg Call correlation without one deterministic subject-device IP is
        # unsafe: emitting the same PCM digits against every B2BUA leg creates false
        # duplicate evidence. Fail closed until provenance is sufficient.
        return [], {"status": "AMBIGUOUS_SUBJECT", "subject_identity": identity}

    subject_ip = str(identity["selected_ip"])
    matched = [call for call in calls if subject_ip in _call_connection_ips(call, packet_result)]
    if len(matched) == 1:
        return matched, {
            "status": "SUBJECT_CALL_SELECTED",
            "subject_identity": identity,
            "selected_call_id": matched[0].get("call_id"),
        }
    return [], {
        "status": "AMBIGUOUS_SUBJECT_CALL",
        "subject_identity": identity,
        "matched_call_ids": [c.get("call_id") for c in matched],
    }


def correlate_pcm_dtmf_with_sip(packet_result: dict, pcm_result: dict, lookback_seconds: float = 15.0) -> list[dict]:
    """Create cross-layer evidence between in-band PCM DTMF and the subject SIP Call.

    A match is evidence that the digits were already present at the PCM RX tap
    before INVITE generation. In multi-leg B2BUA captures, PCM diagnostic UDP
    source provenance must identify exactly one subject Call; the same PCM sequence
    is never copied onto every SIP leg.
    """
    events: list[dict] = []
    candidates = []
    for stream in pcm_result.get("streams", []):
        tap = stream.get("tap", {})
        if str(tap.get("direction", "")).upper() != "RX":
            continue
        for session in stream.get("sessions", []):
            session_start = session.get("start_time")
            if session_start is None:
                continue
            for seq in session.get("dtmf_sequences", []):
                candidates.append({
                    "digits": seq.get("digits", ""),
                    "start_time": session_start + float(seq.get("start_seconds", 0)),
                    "end_time": session_start + float(seq.get("end_seconds", 0)),
                    "tap": tap.get("name"),
                    "session_index": session.get("session_index"),
                    "min_confidence": seq.get("min_confidence"),
                })

    calls, subject_meta = _subject_calls(packet_result, pcm_result)
    for call in calls:
        call_start = call.get("start_time")
        target = _digits_from_uri(call.get("callee"))
        if call_start is None or not target:
            continue
        prior = [c for c in candidates if 0 <= call_start - c["end_time"] <= lookback_seconds]
        if not prior:
            continue
        nearest = min(prior, key=lambda c: call_start - c["end_time"])
        matches = nearest["digits"] == target
        events.append({
            "type": "DTMF_SIP_DIAL_MATCH" if matches else "DTMF_SIP_DIAL_MISMATCH",
            "severity": "INFO" if matches else "MEDIUM",
            "time": nearest["start_time"],
            "evidence_level": "L1" if matches else "L2",
            "scope": {"call_id": call.get("call_id"), "pcm_tap": nearest["tap"], "pcm_session_index": nearest["session_index"]},
            "details": {
                "call_id": call.get("call_id"),
                "sip_target": target,
                "pcm_digits": nearest["digits"],
                "pcm_tap": nearest["tap"],
                "pcm_session_index": nearest["session_index"],
                "pcm_min_confidence": nearest["min_confidence"],
                "lead_time_ms": round((call_start - nearest["end_time"]) * 1000.0, 3),
                "subject_call_selection": subject_meta,
                "interpretation": (
                    "PCM RX在INVITE前已检测到与SIP目标一致的拨号序列"
                    if matches else
                    "PCM RX检测到的拨号序列与随后SIP目标不一致，需要检查DTMF采集/号码组装链路"
                ),
            },
        })
    return events


def _digits_from_uri(uri: str | None) -> str | None:
    if not uri:
        return None
    match = _URI_DIGITS.search(uri)
    return match.group(1) if match else None
