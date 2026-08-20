from __future__ import annotations

from app.analyzers.media.subject_identity import (
    SUBJECT_IDENTITY_UNIQUE,
    infer_pcm_source_device_identity as _infer_pcm_source_device_identity,
)


def _pcm_payload(results: dict[str, dict | None]) -> tuple[dict | None, str | None]:
    pcm = results.get("pcm_intelligence")
    if isinstance(pcm, dict):
        return pcm, "pcm_intelligence"
    nested = (results.get("media_intelligence") or {}).get("pcm")
    if isinstance(nested, dict):
        return nested, "media_intelligence.pcm"
    return None, None


def infer_pcm_source_device_identity(results: dict[str, dict | None]) -> dict:
    pcm, source = _pcm_payload(results)
    return _infer_pcm_source_device_identity(pcm, source=source)


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
    if subject_ip in _sip_ladder_ips(call):
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
        # A display fallback is allowed for usability, but AMBIGUOUS intentionally
        # triggers CALL_BINDING_INCOMPLETE in the report semantic gate.
        "status": "AMBIGUOUS",
        "source_index": index,
        "call": call,
        "selection_rule": "LATEST_RECONSTRUCTED_CALL_BY_END_THEN_START_TIME",
        "subject_identity": identity,
        "scores": scores,
        "fallback_reason": reason,
    }


def select_subject_call(valid_calls: list[tuple[int, dict]], packet: dict, results: dict[str, dict | None]) -> dict:
    identity = infer_pcm_source_device_identity(results)
    if len(valid_calls) == 1:
        index, call = valid_calls[0]
        return {"status": "SELECTED", "source_index": index, "call": call, "selection_rule": "ONLY_RECONSTRUCTED_CALL", "subject_identity": identity, "scores": []}

    subject_ip = identity.get("selected_ip") if identity.get("status") == SUBJECT_IDENTITY_UNIQUE else None
    if not subject_ip:
        return _fallback(valid_calls, identity=identity, scores=[], reason="MULTI_CALL_SUBJECT_DEVICE_IDENTITY_UNAVAILABLE")

    scored = [(index, call, score_call_for_subject(call, packet, str(subject_ip))) for index, call in valid_calls]
    best_score = max((item[2]["score"] for item in scored), default=0)
    winners = [item for item in scored if item[2]["score"] == best_score and best_score > 0]
    if len(winners) != 1:
        return _fallback(valid_calls, identity=identity, scores=[item[2] for item in scored], reason="SUBJECT_DEVICE_IDENTITY_NOT_UNIQUE_TO_ONE_CALL")

    index, call, _score = winners[0]
    return {"status": "SELECTED", "source_index": index, "call": call, "selection_rule": "PCM_SOURCE_DEVICE_IDENTITY_MATCH", "subject_identity": identity, "scores": [item[2] for item in scored]}
