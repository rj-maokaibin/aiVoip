from __future__ import annotations

import hashlib
import wave
from typing import Any, Mapping

from app.reports.human_visuals.wav_window import slice_pcm16_wav_bytes


def render_event_audio_clip(
    wav_bytes: bytes,
    *,
    event: Mapping[str, Any],
    source_artifact: Mapping[str, Any],
    finding_id: str,
    pre_seconds: float = 1.0,
    post_seconds: float = 1.0,
    analyzer_name: str = "EVIDENCE_V2_ARTIFACT_BINDER",
    analyzer_version: str = "2.0",
    profile_version: str = "preliminary-evidence-v2",
) -> tuple[bytes, dict[str, Any]]:
    """Slice a representative PCM16 WAV clip and return canonical provenance.

    ``event.timestamp`` and the source artifact start are absolute seconds in the
    same clock domain. Sample values are never altered; the existing exact WAV
    window helper owns byte-level slicing.
    """

    source_id = _source_id(source_artifact)
    source_start = _source_start(source_artifact)
    event_time = _event_time(event)
    event_id = str(event.get("event_id") or "")
    if not source_id or source_start is None or event_time is None or not event_id:
        return b"", audio_binding_failure(
            finding_id=finding_id,
            event_ref=event_id or None,
            source_artifact_id=source_id,
            source_available=bool(wav_bytes and source_id),
            reason_code="SOURCE_METADATA_INVALID",
        )

    relative_event = event_time - source_start
    requested_start = relative_event - max(0.0, float(pre_seconds))
    requested_end = relative_event + max(0.0, float(post_seconds))

    try:
        clip, window = slice_pcm16_wav_bytes(wav_bytes, requested_start, requested_end)
    except (ValueError, EOFError, wave.Error) as exc:
        return b"", audio_binding_failure(
            finding_id=finding_id,
            event_ref=event_id,
            source_artifact_id=source_id,
            source_available=True,
            reason_code=_render_reason(exc),
        )

    relative_window = window["source_window_seconds"]
    absolute_start = round(source_start + float(relative_window[0]), 6)
    absolute_end = round(source_start + float(relative_window[1]), 6)
    digest = hashlib.sha256(clip).hexdigest()
    artifact_id = "V2CLIP-" + hashlib.sha256(
        f"{source_id}|{event_id}|{absolute_start:.6f}|{absolute_end:.6f}".encode("utf-8")
    ).hexdigest()[:16].upper()

    return clip, {
        "artifact_id": artifact_id,
        "type": "ANOMALY_AUDIO_CLIP",
        "status": "AVAILABLE",
        "artifact_requirement": "AUDIO_CLIP",
        "event_refs": [event_id],
        "finding_refs": [finding_id],
        "source_artifact_ids": [source_id],
        "time_range": {"start": absolute_start, "end": absolute_end},
        "sha256": digest,
        "size": len(clip),
        "mime_type": "audio/wav",
        "analyzer_name": analyzer_name,
        "analyzer_version": analyzer_version,
        "profile_version": profile_version,
        "provenance_required": True,
        "window": window,
    }


def audio_binding_failure(
    *,
    finding_id: str,
    event_ref: str | None,
    source_artifact_id: str | None,
    source_available: bool,
    reason_code: str,
) -> dict[str, Any]:
    """Return a structured failure instead of ambiguous 'no matching audio'."""

    return {
        "artifact_requirement": "AUDIO_CLIP",
        "status": "FAILED",
        "reason_code": str(reason_code).upper(),
        "source_available": bool(source_available),
        "finding_refs": [finding_id],
        "event_refs": [event_ref] if event_ref else [],
        "source_artifact_ids": [source_artifact_id] if source_artifact_id else [],
    }


def source_unavailable_audio_binding(*, finding_id: str, event_ref: str | None = None) -> dict[str, Any]:
    return audio_binding_failure(
        finding_id=finding_id,
        event_ref=event_ref,
        source_artifact_id=None,
        source_available=False,
        reason_code="SOURCE_UNAVAILABLE",
    )


def _source_id(source: Mapping[str, Any]) -> str | None:
    value = source.get("artifact_id") or source.get("source_artifact_id")
    return str(value) if value else None


def _source_start(source: Mapping[str, Any]) -> float | None:
    value = source.get("start_time")
    if value is None:
        time_range = source.get("time_range") or {}
        if isinstance(time_range, Mapping):
            value = time_range.get("start")
    return float(value) if value is not None else None


def _event_time(event: Mapping[str, Any]) -> float | None:
    value = event.get("timestamp")
    if value is None:
        value = event.get("absolute_time")
    return float(value) if value is not None else None


def _render_reason(exc: BaseException) -> str:
    if isinstance(exc, ValueError) and "PCM16" in str(exc).upper():
        return "UNSUPPORTED_CODEC"
    return "RENDER_ERROR"

