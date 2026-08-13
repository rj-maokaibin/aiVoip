from __future__ import annotations

import re

_URI_DIGITS = re.compile(r"(?:sip:|tel:)?([0-9*#]+)")


def correlate_pcm_dtmf_with_sip(packet_result: dict, pcm_result: dict, lookback_seconds: float = 15.0) -> list[dict]:
    """Create cross-layer evidence between in-band PCM DTMF and SIP dial target.

    A match is evidence that the digits were already present at the PCM RX tap
    before INVITE generation. It does not by itself prove downstream DTMF
    handling is correct.
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
    for call in packet_result.get("calls", []):
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
            "details": {
                "call_id": call.get("call_id"),
                "sip_target": target,
                "pcm_digits": nearest["digits"],
                "pcm_tap": nearest["tap"],
                "pcm_session_index": nearest["session_index"],
                "pcm_min_confidence": nearest["min_confidence"],
                "lead_time_ms": round((call_start - nearest["end_time"]) * 1000.0, 3),
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
