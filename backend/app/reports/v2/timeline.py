from __future__ import annotations

from typing import Any, Iterable, Mapping



def build_timeline_v2(
    call: Mapping[str, Any],
    rtp_streams: Iterable[Mapping[str, Any]],
    *,
    pcm_windows: Mapping[str, Mapping[str, Any]] | None = None,
    capture_window: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build report V2 timeline facts from observed evidence windows.

    Media observation time comes only from RTP observations. SIP ACK may be an
    establishment anchor but is never used as the end of an active-media
    window. PCM/capture windows remain separate dimensions so that visibility
    and capture completeness are not collapsed into one ambiguous range.
    """

    stream_windows: list[dict[str, Any]] = []
    for stream in rtp_streams:
        packet_count = int(stream.get("packet_count") or 0)
        start = _number(stream.get("start_time"))
        end = _number(stream.get("end_time"))
        if packet_count <= 0 or start is None or end is None:
            continue
        stream_windows.append(
            {
                "stream_id": stream.get("stream_id"),
                "start": start,
                "end": end,
                "packet_count": packet_count,
                "src_ip": stream.get("src_ip"),
                "src_port": stream.get("src_port"),
                "dst_ip": stream.get("dst_ip"),
                "dst_port": stream.get("dst_port"),
            }
        )

    media_start = min((item["start"] for item in stream_windows), default=None)
    media_end = max((item["end"] for item in stream_windows), default=None)
    media_duration = None
    if media_start is not None and media_end is not None:
        media_duration = round(media_end - media_start, 6)

    signaling_start = _number(call.get("invite_time"))
    signaling_end = _number(call.get("capture_last_signaling_time"))

    normalized_pcm: dict[str, dict[str, Any]] = {}
    for name, window in (pcm_windows or {}).items():
        start = _number(window.get("start"))
        end = _number(window.get("end"))
        normalized_pcm[str(name)] = {
            "start": start,
            "end": end,
            "duration_seconds": round(end - start, 6)
            if start is not None and end is not None
            else None,
        }

    normalized_capture = {
        "start": _number((capture_window or {}).get("start")),
        "end": _number((capture_window or {}).get("end")),
    }
    if normalized_capture["start"] is not None and normalized_capture["end"] is not None:
        normalized_capture["duration_seconds"] = round(
            normalized_capture["end"] - normalized_capture["start"], 6
        )
    else:
        normalized_capture["duration_seconds"] = None

    return {
        "schema": "evidence-timeline-v2",
        "capture_window": normalized_capture,
        "signaling_window": {
            "start": signaling_start,
            "end": signaling_end,
            "duration_seconds": round(signaling_end - signaling_start, 6)
            if signaling_start is not None and signaling_end is not None
            else None,
        },
        "established_time": _number(call.get("established_time")),
        "termination": call.get("termination") or {},
        "call_end_time": _number(call.get("call_end_time")),
        "media_observation_window": {
            "start": media_start,
            "end": media_end,
            "duration_seconds": media_duration,
            "source": "RTP_OBSERVATION" if stream_windows else "UNAVAILABLE",
            "stream_count": len(stream_windows),
        },
        "rtp_stream_windows": stream_windows,
        "pcm_observation_windows": normalized_pcm,
    }


def event_relative_time(event_time: float | None, anchor_time: float | None) -> float | None:
    """Return a stable relative timestamp for event cards and validators."""
    if event_time is None or anchor_time is None:
        return None
    return round(float(event_time) - float(anchor_time), 6)


def _number(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)
