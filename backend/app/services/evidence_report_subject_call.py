from __future__ import annotations

from collections import defaultdict
from typing import Any


SUBJECT_IDENTITY_UNAVAILABLE = "UNAVAILABLE"
SUBJECT_IDENTITY_UNIQUE = "UNIQUE"
SUBJECT_IDENTITY_AMBIGUOUS = "AMBIGUOUS"


def _pcm_payload(results: dict[str, dict | None]) -> tuple[dict | None, str | None]:
    pcm = results.get("pcm_intelligence")
    if isinstance(pcm, dict):
        return pcm, "pcm_intelligence"
    nested = (results.get("media_intelligence") or {}).get("pcm")
    if isinstance(nested, dict):
        return nested, "media_intelligence.pcm"
    return None, None


def infer_pcm_source_device_identity(results: dict[str, dict | None]) -> dict:
    """Infer the device emitting diagnostic PCM from packet provenance only."""
    pcm, source = _pcm_payload(results)
    if not pcm:
        return {
            "status": SUBJECT_IDENTITY_UNAVAILABLE,
            "source": source,
            "candidate_ips": [],
            "selected_ip": None,
            "reason": "PCM_RESULT_UNAVAILABLE",
        }

    by_ip: dict[str, dict[str, Any]] = {}
    populated_taps: set[str] = set()
    for stream in pcm.get("streams", []) or []:
        tap_name = str((stream.get("tap") or {}).get("name") or "")
        if int(stream.get("packet_count") or 0) > 0:
            populated_taps.add(tap_name)
        for endpoint in stream.get("source_endpoints", []) or []:
            ip = str(endpoint.get("ip") or "").strip()
            if not ip:
                continue
            row = by_ip.setdefault(ip, {"ip": ip, "packet_count": 0, "taps": set(), "ports": set()})
            row["packet_count"] += int(endpoint.get("packet_count") or 0)
            if tap_name:
                row["taps"].add(tap_name)
            if endpoint.get("port") is not None:
                row["ports"].add(int(endpoint["port"]))

    candidates = [
        {
            "ip": row["ip"],
            "packet_count": row["packet_count"],
            "taps": sorted(row["taps"]),
            "ports": sorted(row["ports"]),
        }
        for row in by_ip.values()
    ]
    candidates.sort(key=lambda row: (-int(row["packet_count"]), row["ip"]))
    if not candidates:
        return {
            "status": SUBJECT_IDENTITY_UNAVAILABLE,
            "source": source,
            "candidate_ips": [],
            "selected_ip": None,
            "reason": "PCM_SOURCE_ENDPOINTS_UNAVAILABLE",
        }

    complete = [row for row in candidates if populated_taps and set(row["taps"]) >= populated_taps]
    if len(complete) == 1:
        return {
            "status": SUBJECT_IDENTITY_UNIQUE,
            "source": source,
            "candidate_ips": candidates,
            "selected_ip": complete[0]["ip"],
            "populated_taps": sorted(populated_taps),
            "reason": "ONE_PCM_SOURCE_IP_COVERS_ALL_POPULATED_TAPS",
        }
    if len(candidates) == 1:
        return {
            "status": SUBJECT_IDENTITY_UNIQUE,
            "source": source,
            "candidate_ips": candidates,
            "selected_ip": candidates[0]["ip"],
            "populated_taps": sorted(populated_taps),
            "reason": "ONLY_ONE_PCM_SOURCE_IP",
        }
    return {
        "status": SUBJECT_IDENTITY_AMBIGUOUS,
        "source": source,
        "candidate_ips": candidates,
        "selected_ip": None,
        "populated_taps": sorted(populated_taps),
        "reason": "MULTIPLE_PCM_SOURCE_DEVICE_CANDIDATES",
    }


def _sdp_connection_ips(call: dict) -> set[str]:
    ips: set[str] = set()
    sdp = call.get("sdp") or {}
    for side in ("offer", "answer"):
        payload = sdp.get(side) or {}
        connection = payload.get("connection_address")
        if connection:
            ips.add(str(connection))
        for media in payload.get("media", []) or []:
            connection = media.get("connection_address")
            if connection:
                ips.add(str(connection))
    return ips


def _call_rtp_streams(call: dict, packet: dict) -> list[dict]:
    ids = set(call.get("rtp_stream_ids") or [])
    return [s for s in packet.get("rtp_streams", []) or [] if s.get("stream_id") in ids]


def _sip_ladder_ips(call: dict) -> set[str]:
    ips: set[str] = set()
    for item in call.get("ladder", []) or []:
        for key in ("src", "dst"):
            raw = str(item.get(key) or "")
            if raw.count(":") == 1:
                ip, _port = raw.rsplit(":", 1)
                if ip:
                    ips.add(ip)
    return ips


def score_call_for_subject(call: dict, packet: dict, subject_ip: str) -> dict:
    """Score deterministic evidence that a SIP leg is the DUT-facing subject leg."""
    reasons: list[dict] = []
    score = 0
    sdp_ips = _sdp_connection_ips(call)
    if subject_ip in sdp_ips:
        score += 100
        reasons.append({"type": "SDP_CONNECTION_MATCH", "weight": 100})

    streams = _call_rtp_streams(call, packet)
    subject_streams = [s for s in streams if subject_ip in {str(s.get("src_ip") or ""), str(s.get("dst_ip") or "")}]
    if subject_streams:
        weight = 50 + min(20, 5 * len(subject_streams))
        score += weight
        reasons.append({"type": "RTP_ENDPOINT_MATCH", "weight": weight, "stream_ids": [s.get("stream_id") for s in subject_streams]})

    ladder_ips = _sip_ladder_ips(call)
    if subject_ip in ladder_ips:
        score += 10
        reasons.append({"type": "SIP_SIGNALING_ENDPOINT_MATCH", "weight": 10})

    return {
        "call_id": call.get("call_id"),
        "score": score,
        "subject_ip": subject_ip,
        "sdp_connection_ips": sorted(sdp_ips),
        "matched_rtp_stream_ids": [s.get("stream_id") for s in subject_streams],
        "reasons": reasons,
    }


def _latest_call(valid_calls: list[tuple[int, dict]]) -> tuple[int, dict]:
    def key(item: tuple[int, dict]):
        index, call = item
        end = call.get("media_end_time") if call.get("media_end_time") is not None else call.get("end_time")
        start = call.get("media_start_time") if call.get("media_start_time") is not None else call.get("start_time")
        try:
            end_value = float(end) if end is not None else float("-inf")
        except (TypeError, ValueError):
            end_value = float("-inf")
        try:
            start_value = float(start) if start is not None else float("-inf")
        except (TypeError, ValueError):
            start_value = float("-inf")
        return end_value, start_value, index
    return max(valid_calls, key=key)


def _fallback(valid_calls: list[tuple[int, dict]], *, identity: dict, scores: list[dict], reason: str) -> dict:
    index, call = _latest_call(valid_calls)
    return {
        "status": "FALLBACK_UNVERIFIED",
        "source_index": index,
        "call": call,
        "selection_rule": "LATEST_RECONSTRUCTED_CALL_BY_END_THEN_START_TIME",
        "subject_identity": identity,
        "scores": scores,
        "fallback_reason": reason,
    }


def select_subject_call(valid_calls: list[tuple[int, dict]], packet: dict, results: dict[str, dict | None]) -> dict:
    """Select a diagnostic Call; unverified latest-leg fallback is never authoritative."""
    identity = infer_pcm_source_device_identity(results)
    if len(valid_calls) == 1:
        index, call = valid_calls[0]
        return {
            "status": "SELECTED",
            "source_index": index,
            "call": call,
            "selection_rule": "ONLY_RECONSTRUCTED_CALL",
            "subject_identity": identity,
            "scores": [],
        }

    subject_ip = identity.get("selected_ip") if identity.get("status") == SUBJECT_IDENTITY_UNIQUE else None
    if not subject_ip:
        return _fallback(valid_calls, identity=identity, scores=[], reason="MULTI_CALL_SUBJECT_DEVICE_IDENTITY_UNAVAILABLE")

    scored = [(index, call, score_call_for_subject(call, packet, str(subject_ip))) for index, call in valid_calls]
    best_score = max((item[2]["score"] for item in scored), default=0)
    winners = [item for item in scored if item[2]["score"] == best_score and best_score > 0]
    if len(winners) != 1:
        return _fallback(valid_calls, identity=identity, scores=[item[2] for item in scored], reason="SUBJECT_DEVICE_IDENTITY_NOT_UNIQUE_TO_ONE_CALL")

    index, call, _score = winners[0]
    return {
        "status": "SELECTED",
        "source_index": index,
        "call": call,
        "selection_rule": "PCM_SOURCE_DEVICE_IDENTITY_MATCH",
        "subject_identity": identity,
        "scores": [item[2] for item in scored],
    }
