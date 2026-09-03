from __future__ import annotations

from typing import Any, Mapping


PRE_CALL = "PRE_CALL"
SIGNALING = "SIGNALING"
ACTIVE_MEDIA = "ACTIVE_MEDIA"
POST_MEDIA = "POST_MEDIA"
UNSCOPED = "UNSCOPED"


def classify_event_phase(
    event_time: float | None,
    *,
    call: Mapping[str, Any],
    timeline: Mapping[str, Any],
) -> str:
    """Classify an event against deterministic call/media observation anchors."""

    if event_time is None:
        return UNSCOPED
    when = float(event_time)
    invite = _number(call.get("invite_time"))
    media = timeline.get("media_observation_window") or {}
    media_start = _number(media.get("start")) if isinstance(media, Mapping) else None
    media_end = _number(media.get("end")) if isinstance(media, Mapping) else None

    if invite is not None and when < invite:
        return PRE_CALL
    if media_start is not None and when < media_start:
        return SIGNALING
    if media_start is not None and media_end is not None and media_start <= when <= media_end:
        return ACTIVE_MEDIA
    if media_end is not None and when > media_end:
        return POST_MEDIA
    if invite is not None and when >= invite:
        return SIGNALING
    return UNSCOPED


def finding_class_for_phase(observation_type: str, phase: str) -> str:
    """Do not count out-of-media timing spikes as primary call-media failures."""

    observation = str(observation_type or "").upper()
    phase = str(phase or UNSCOPED).upper()
    timing = observation in {
        "PACKET_INTERVAL_SPIKE",
        "BURST_AFTER_DELAY",
        "RTP_HIGH_DELTA",
        "PCM_PACKET_INTERVAL_SPIKE",
    }
    if timing and phase != ACTIVE_MEDIA:
        return "UNCERTAIN"
    return "ABNORMAL"


def _number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
