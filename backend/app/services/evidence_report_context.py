from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any


ANALYSIS_MODE_REPRODUCTION = "REPRODUCTION"
ANALYSIS_MODE_OFFLINE_EVIDENCE = "OFFLINE_EVIDENCE"
CALL_SOURCE_REPRODUCTION = "REPRODUCTION_CALL"
CALL_SOURCE_PACKET_RECONSTRUCTION = "PACKET_RECONSTRUCTION"


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


def _packet_result_with_calls(results: dict[str, dict | None]) -> tuple[dict | None, str | None]:
    packet = results.get("packet_intelligence") or {}
    if packet.get("calls"):
        return packet, "packet_intelligence"

    # Media Intelligence embeds the Packet Intelligence result used for its own
    # cross-layer analysis.  This is a safe fallback when a dedicated Packet
    # AnalyzerRun is unavailable or contains no reconstructed Call rows.
    media = results.get("media_intelligence") or {}
    nested = media.get("packet") or {}
    if nested.get("calls"):
        return nested, "media_intelligence.packet"
    return (packet if packet else None), ("packet_intelligence" if packet else None)


def _packet_call_sort_key(item: tuple[int, dict]) -> tuple[float, float, int]:
    index, call = item
    end = _as_float(call.get("media_end_time"))
    if end is None:
        end = _as_float(call.get("end_time"))
    start = _as_float(call.get("media_start_time"))
    if start is None:
        start = _as_float(call.get("start_time"))
    return (end if end is not None else float("-inf"), start if start is not None else float("-inf"), index)


def _packet_call_id(call: dict, *, index: int) -> str:
    material = "|".join(
        [
            str(call.get("call_id") or ""),
            str(call.get("start_time") or ""),
            str(call.get("end_time") or ""),
            str(index),
        ]
    )
    return "packet-call-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:20]


def _serialize_packet_call(call: dict, *, ordinal: int, total: int, analyzer_source: str) -> dict:
    start_epoch = _as_float(call.get("start_time"))
    end_epoch = _as_float(call.get("end_time"))
    media_start_epoch = _as_float(call.get("media_start_time"))
    media_end_epoch = _as_float(call.get("media_end_time"))
    external_ref = call.get("call_id")
    return {
        "id": _packet_call_id(call, index=ordinal),
        "call_no": f"PCAP-{ordinal + 1}",
        "external_call_ref": external_ref,
        "sip_call_id": external_ref,
        "status": call.get("state") or "UNKNOWN",
        "verdict": None,
        "role": "OFFLINE_RECONSTRUCTED",
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
            "type": CALL_SOURCE_PACKET_RECONSTRUCTION,
            "analyzer_result": analyzer_source,
            "selection_ordinal": ordinal + 1,
            "reconstructed_call_count": total,
        },
    }


def select_reconstructed_packet_call(results: dict[str, dict | None]) -> tuple[dict | None, dict]:
    packet, analyzer_source = _packet_result_with_calls(results)
    calls = list((packet or {}).get("calls") or [])
    valid = [(idx, call) for idx, call in enumerate(calls) if isinstance(call, dict) and call.get("call_id")]
    if not valid:
        return None, {
            "call_source": None,
            "reconstructed_call_count": 0,
            "selection_rule": "NO_RECONSTRUCTABLE_PACKET_CALL",
            "packet_call_source": analyzer_source,
        }

    selected_index, selected = max(valid, key=_packet_call_sort_key)
    serialized = _serialize_packet_call(
        selected,
        ordinal=selected_index,
        total=len(valid),
        analyzer_source=analyzer_source or "packet_intelligence",
    )
    return serialized, {
        "call_source": CALL_SOURCE_PACKET_RECONSTRUCTION,
        "reconstructed_call_count": len(valid),
        "selected_sip_call_id": selected.get("call_id"),
        "selection_rule": "LATEST_RECONSTRUCTED_CALL_BY_END_THEN_START_TIME",
        "packet_call_source": analyzer_source,
    }


def build_analysis_context(
    *,
    scope_type: str,
    session: dict | None,
    reproduction_call: dict | None,
    results: dict[str, dict | None],
) -> dict:
    """Resolve the report-facing analysis context without fabricating DB entities.

    ReproductionSession/ReproductionCall remain authoritative when present.  For
    offline CASE evidence, a SIP Call reconstructed deterministically by Packet
    Intelligence is projected into the Canonical Report only; it is deliberately
    not persisted as a ReproductionCall foreign-key row.
    """
    if reproduction_call is not None:
        call = dict(reproduction_call)
        call.setdefault("source", {"type": CALL_SOURCE_REPRODUCTION})
        return {
            "mode": ANALYSIS_MODE_REPRODUCTION,
            "scope_type": str(scope_type).upper(),
            "session": session,
            "call": call,
            "call_source": CALL_SOURCE_REPRODUCTION,
            "offline": False,
            "reconstructed_call_count": 0,
            "selection_rule": "REPRODUCTION_CALL_AUTHORITATIVE",
        }

    reconstructed, metadata = select_reconstructed_packet_call(results)
    return {
        "mode": ANALYSIS_MODE_OFFLINE_EVIDENCE,
        "scope_type": str(scope_type).upper(),
        "session": session,
        "call": reconstructed,
        "call_source": metadata.get("call_source"),
        "offline": True,
        **metadata,
    }
