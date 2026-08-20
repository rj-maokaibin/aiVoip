from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from app.contracts.evidence_report import AnalysisMode, CallOrigin, CallScope


REPORT_SEMANTIC_CONTRADICTION = "REPORT_SEMANTIC_CONTRADICTION"
CALL_BINDING_INCOMPLETE = "CALL_BINDING_INCOMPLETE"
FULLY_REVIEWABLE = "FULLY_REVIEWABLE"
NOT_FULLY_REVIEWABLE = "NOT_FULLY_REVIEWABLE"


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _epoch_iso(value: Any) -> str | None:
    epoch = _as_float(value)
    if epoch is None:
        return None
    try:
        return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _sip_user(uri: Any) -> str | None:
    if not uri:
        return None
    text = str(uri).strip().strip("<>")
    lower = text.lower()
    pos = lower.find("sips:")
    prefix_len = 5
    if pos < 0:
        pos = lower.find("sip:")
        prefix_len = 4
    if pos >= 0:
        text = text[pos + prefix_len:]
    for sep in ("@", ";", ">", "?"):
        if sep in text:
            text = text.split(sep, 1)[0]
    text = text.strip()
    return text or None


def _capture_origin(evidences: list[dict]) -> str:
    sources = [str(x.get("source") or "").strip().upper() for x in evidences if x.get("source")]
    if any(x in {"USER_UPLOAD", "MANUAL_UPLOAD", "UPLOAD"} for x in sources):
        return "USER_UPLOAD"
    if sources:
        return sources[0]
    return "UNKNOWN"


def _packet_result_with_calls(results: dict[str, dict | None]) -> tuple[dict | None, str | None]:
    packet = results.get("packet_intelligence") or {}
    if packet.get("calls"):
        return packet, "packet_intelligence"

    # Media Intelligence embeds the Packet Intelligence result used for its own
    # cross-layer analysis. This is a provenance-preserving fallback only; SIP is
    # not re-analysed here.
    media = results.get("media_intelligence") or {}
    nested = media.get("packet") or {}
    if nested.get("calls"):
        return nested, "media_intelligence.packet"
    return (packet if packet else nested if nested else None), (
        "packet_intelligence" if packet else "media_intelligence.packet" if nested else None
    )


def _packet_call_sort_key(item: tuple[int, dict]) -> tuple[float, float, int]:
    index, call = item
    end = _as_float(call.get("media_end_time"))
    if end is None:
        end = _as_float(call.get("end_time"))
    start = _as_float(call.get("media_start_time"))
    if start is None:
        start = _as_float(call.get("start_time"))
    return (
        end if end is not None else float("-inf"),
        start if start is not None else float("-inf"),
        index,
    )


def _stable_packet_call_id(call: dict, *, source_index: int) -> str:
    material = "|".join(
        [
            str(call.get("call_id") or ""),
            str(call.get("start_time") or ""),
            str(call.get("end_time") or ""),
            str(source_index),
        ]
    )
    return "packet-call-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:20]


def _serialize_packet_call(
    call: dict,
    *,
    display_ordinal: int,
    source_index: int,
    total: int,
    analyzer_source: str,
) -> dict:
    start_epoch = _as_float(call.get("start_time"))
    end_epoch = _as_float(call.get("end_time"))
    media_start_epoch = _as_float(call.get("media_start_time"))
    media_end_epoch = _as_float(call.get("media_end_time"))
    external_ref = call.get("call_id")
    caller_uri = call.get("caller")
    callee_uri = call.get("callee")
    display_id = f"CALL-{display_ordinal + 1:03d}"
    return {
        "id": display_id,
        "stable_id": _stable_packet_call_id(call, source_index=source_index),
        "call_no": display_id,
        "external_call_ref": external_ref,
        "sip_call_id": external_ref,
        "origin": CallOrigin.RECONSTRUCTED_FROM_PCAP.value,
        "status": call.get("state") or "UNKNOWN",
        "verdict": None,
        "role": "OFFLINE_RECONSTRUCTED",
        "caller": _sip_user(caller_uri),
        "caller_uri": caller_uri,
        "dialed_number": _sip_user(callee_uri),
        "callee_uri": callee_uri,
        "started_at": _epoch_iso(start_epoch),
        "ended_at": _epoch_iso(end_epoch),
        "started_at_epoch": start_epoch,
        "ended_at_epoch": end_epoch,
        "media_started_at": _epoch_iso(media_start_epoch),
        "media_ended_at": _epoch_iso(media_end_epoch),
        "media_started_at_epoch": media_start_epoch,
        "media_ended_at_epoch": media_end_epoch,
        "incomplete": end_epoch is None,
        "invite_final_status": call.get("invite_final_status"),
        "rtp_stream_ids": list(call.get("rtp_stream_ids") or []),
        "media_direction_health": call.get("media_direction_health") or {},
        "capture_completeness": call.get("capture_completeness") or {},
        "source": {
            "type": CallOrigin.RECONSTRUCTED_FROM_PCAP.value,
            "analyzer_result": analyzer_source,
            "source_index": source_index,
            "display_ordinal": display_ordinal + 1,
            "reconstructed_call_count": total,
        },
    }


def _packet_call_count(packet: dict | None) -> int:
    packet = packet or {}
    summary = packet.get("summary") or {}
    value = summary.get("call_count")
    if value is not None:
        try:
            return int(value)
        except (TypeError, ValueError):
            pass
    return len(packet.get("calls") or [])


def _has_bidirectional_call_media(packet: dict | None) -> bool:
    for call in (packet or {}).get("calls", []) or []:
        health = call.get("media_direction_health") or {}
        if health.get("status") == "BIDIRECTIONAL":
            return True
    return False


def _has_rtp_media(packet: dict | None) -> bool:
    packet = packet or {}
    if packet.get("rtp_streams"):
        return True
    summary = packet.get("summary") or {}
    try:
        return int(summary.get("rtp_stream_count") or 0) > 0
    except (TypeError, ValueError):
        return False


def select_reconstructed_packet_call(results: dict[str, dict | None]) -> tuple[dict | None, dict]:
    packet, analyzer_source = _packet_result_with_calls(results)
    calls = list((packet or {}).get("calls") or [])
    valid = [(idx, call) for idx, call in enumerate(calls) if isinstance(call, dict) and call.get("call_id")]
    if not valid:
        return None, {
            "reconstructed_call_count": 0,
            "selected_sip_call_id": None,
            "selection_rule": "NO_RECONSTRUCTABLE_PACKET_CALL",
            "packet_call_source": analyzer_source,
            "packet_call_count": _packet_call_count(packet),
            "bidirectional_call_media": _has_bidirectional_call_media(packet),
            "rtp_media_present": _has_rtp_media(packet),
        }

    selected_source_index, selected = max(valid, key=_packet_call_sort_key)
    display_ordinal = next(i for i, item in enumerate(valid) if item[0] == selected_source_index)
    serialized = _serialize_packet_call(
        selected,
        display_ordinal=display_ordinal,
        source_index=selected_source_index,
        total=len(valid),
        analyzer_source=analyzer_source or "packet_intelligence",
    )
    return serialized, {
        "reconstructed_call_count": len(valid),
        "selected_sip_call_id": selected.get("call_id"),
        "selection_rule": "LATEST_RECONSTRUCTED_CALL_BY_END_THEN_START_TIME",
        "packet_call_source": analyzer_source,
        "packet_call_count": _packet_call_count(packet),
        "bidirectional_call_media": _has_bidirectional_call_media(packet),
        "rtp_media_present": _has_rtp_media(packet),
    }


def _runtime_display_call(runtime_call: dict) -> dict:
    out = dict(runtime_call)
    out["origin"] = CallOrigin.REPRODUCTION_RUNTIME.value
    out.setdefault("source", {"type": CallOrigin.REPRODUCTION_RUNTIME.value})
    return out


def _semantic_issues(
    *,
    mode: AnalysisMode,
    session: dict | None,
    runtime_call: dict | None,
    display_call: dict | None,
    metadata: dict,
) -> list[str]:
    issues: list[str] = []
    if int(metadata.get("packet_call_count") or 0) > 0 and display_call is None:
        issues.append(REPORT_SEMANTIC_CONTRADICTION)
    if bool(metadata.get("bidirectional_call_media")) and display_call is None:
        issues.append(CALL_BINDING_INCOMPLETE)
    if mode == AnalysisMode.REPRODUCTION and session is not None and runtime_call is None and display_call is not None:
        issues.append(CALL_BINDING_INCOMPLETE)
    return list(dict.fromkeys(issues))


def resolve_report_analysis_context(
    *,
    scope_type: str,
    session: dict | None,
    runtime_call: dict | None,
    evidences: list[dict],
    results: dict[str, dict | None],
) -> dict:
    """Normalize runtime/offline facts into report semantics without fabricating DB rows.

    The resolver never re-runs SIP analysis and never creates ReproductionCall.
    Runtime Call is authoritative when present. Otherwise it may project the
    deterministic Packet Analyzer Call into ``display_call`` for reviewability.
    """
    mode = AnalysisMode.REPRODUCTION if session is not None else AnalysisMode.OFFLINE_IMPORTED
    reconstructed, metadata = select_reconstructed_packet_call(results)

    if runtime_call is not None:
        display_call = _runtime_display_call(runtime_call)
        call_origin = CallOrigin.REPRODUCTION_RUNTIME
        selection_rule = "REPRODUCTION_RUNTIME_CALL_AUTHORITATIVE"
    elif reconstructed is not None:
        display_call = reconstructed
        call_origin = CallOrigin.RECONSTRUCTED_FROM_PCAP
        selection_rule = metadata.get("selection_rule")
    else:
        display_call = None
        call_origin = CallOrigin.MEDIA_SESSION_UNBOUND if metadata.get("rtp_media_present") else None
        selection_rule = metadata.get("selection_rule")

    if display_call is not None:
        call_scope = CallScope.BOUND
    elif metadata.get("rtp_media_present"):
        call_scope = CallScope.UNBOUND
    else:
        call_scope = CallScope.NOT_APPLICABLE

    issues = _semantic_issues(
        mode=mode,
        session=session,
        runtime_call=runtime_call,
        display_call=display_call,
        metadata=metadata,
    )
    analysis_context = {
        "analysis_mode": mode.value,
        "capture_origin": _capture_origin(evidences),
        "source_session_id": session.get("id") if session else None,
        "call_reconstruction": "ENABLED",
        "reconstructed_call_count": metadata.get("reconstructed_call_count", 0),
        "packet_call_count": metadata.get("packet_call_count", 0),
        "call_origin": call_origin.value if call_origin else None,
        "call_scope": call_scope.value,
        "selection_rule": selection_rule,
        "packet_call_source": metadata.get("packet_call_source"),
        "selected_sip_call_id": metadata.get("selected_sip_call_id"),
        "semantic_status": "OK" if not issues else "INCOMPLETE",
        "semantic_issues": issues,
        "reviewability": FULLY_REVIEWABLE if not issues else NOT_FULLY_REVIEWABLE,
    }
    return {"analysis_context": analysis_context, "display_call": display_call}


# Temporary compatibility alias for the first PR1 draft. New code should call
# resolve_report_analysis_context so the contract name stays explicit.
def build_analysis_context(
    *,
    scope_type: str,
    session: dict | None,
    reproduction_call: dict | None,
    results: dict[str, dict | None],
) -> dict:
    resolved = resolve_report_analysis_context(
        scope_type=scope_type,
        session=session,
        runtime_call=reproduction_call,
        evidences=[],
        results=results,
    )
    context = dict(resolved["analysis_context"])
    context.update({"mode": context["analysis_mode"], "offline": context["analysis_mode"] == AnalysisMode.OFFLINE_IMPORTED.value,
                    "session": session, "call": resolved["display_call"], "call_source": context.get("call_origin")})
    return context
