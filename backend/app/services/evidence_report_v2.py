from __future__ import annotations

from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AnalyzerRun, Artifact
from app.reports.v2.artifact_binding import render_event_audio_clip, source_unavailable_audio_binding
from app.reports.v2.composer import build_first_page
from app.reports.v2.runtime_adapter import compose_v2_runtime_from_analyzers
from app.reports.v2.semantic_validator import validate_report_semantics
from app.services.evidence_report_artifacts import persist_artifact


V2_SCHEMA = "preliminary-evidence-report-v2"


def visual_source_results(results: Mapping[str, Mapping[str, Any] | None]) -> dict[str, Mapping[str, Any] | None]:
    """Resolve packet/PCM sources with the same standalone-first contract as V1."""
    resolved = dict(results)
    media = results.get("media_intelligence") or {}
    if isinstance(media, Mapping):
        if resolved.get("packet_intelligence") is None:
            resolved["packet_intelligence"] = media.get("packet")
        if resolved.get("pcm_intelligence") is None:
            resolved["pcm_intelligence"] = media.get("pcm")
    return resolved


def compose_v2_runtime_payload(
    *,
    report_id: str,
    results: Mapping[str, Mapping[str, Any] | None],
    analysis_context: Mapping[str, Any],
) -> dict[str, Any]:
    """Compose V2 from deterministic analyzer facts selected by the V1 context authority."""
    resolved = visual_source_results(results)
    packet = resolved.get("packet_intelligence") or {}
    pcm = resolved.get("pcm_intelligence") or {}
    media = results.get("media_intelligence") or {}
    selected_id = str(analysis_context.get("selected_sip_call_id") or "")
    calls = [item for item in packet.get("calls") or [] if isinstance(item, Mapping)]
    selected = next((item for item in calls if str(item.get("call_id") or "") == selected_id), None)
    if selected is None:
        raise ValueError("EVIDENCE_V2_SELECTED_SIP_CALL_NOT_FOUND")

    capture_window = _capture_window(packet, pcm)
    return compose_v2_runtime_from_analyzers(
        report_id=report_id,
        sip_call=selected,
        packet=packet,
        pcm=pcm,
        media=media,
        subject_device_ip=analysis_context.get("subject_device_ip"),
        capture_window=capture_window,
    )


def _pcm_source_run(
    results: Mapping[str, Mapping[str, Any] | None],
    runs: Mapping[str, AnalyzerRun | None],
) -> AnalyzerRun | None:
    """Return the AnalyzerRun that owns the PCM facts selected by visual_source_results.

    Standalone PCM is authoritative when present, including an intentionally empty
    result object. Only when standalone PCM is absent may Media Intelligence's nested
    PCM projection own the persisted PCM_WAV artifacts. Keeping fact-source and
    artifact-source selection identical prevents false AUDIO_SOURCE_UNAVAILABLE.
    """
    if results.get("pcm_intelligence") is not None:
        return runs.get("pcm_intelligence")
    media = results.get("media_intelligence")
    if isinstance(media, Mapping) and media.get("pcm") is not None:
        return runs.get("media_intelligence")
    return runs.get("pcm_intelligence") or runs.get("media_intelligence")


def bind_v2_anomaly_audio(
    db: Session,
    storage,
    *,
    report_row,
    v2: dict[str, Any],
    results: Mapping[str, Mapping[str, Any] | None],
    runs: Mapping[str, AnalyzerRun | None],
) -> list[Artifact]:
    """Bind ACTIVE_MEDIA PCM timing evidence to the exact analyzer PCM WAV when available."""
    resolved = visual_source_results(results)
    pcm = resolved.get("pcm_intelligence") or {}
    pcm_run = _pcm_source_run(results, runs)
    wavs = _pcm_wavs(db, pcm_run)
    event_by_id = {
        str(item.get("event_id")): item
        for item in v2.get("events") or []
        if isinstance(item, Mapping) and item.get("event_id")
    }
    artifacts = list(v2.get("artifacts") or [])
    failures = list(v2.get("artifact_failures") or [])
    created: list[Artifact] = []

    for finding in v2.get("findings") or []:
        if not isinstance(finding, dict):
            continue
        if str(finding.get("class") or "ABNORMAL").upper() != "ABNORMAL":
            continue
        candidate = _active_pcm_event(finding, event_by_id)
        if candidate is None:
            continue
        finding_id = str(finding.get("finding_id") or "")
        if not finding_id:
            continue
        finding["artifact_requirements"] = sorted(set(finding.get("artifact_requirements") or []) | {"AUDIO_CLIP"})
        wav = _matching_pcm_wav(wavs, candidate)
        if wav is None:
            finding["audio_source_available"] = False
            failures.append(source_unavailable_audio_binding(finding_id=finding_id, event_ref=str(candidate.get("event_id") or "") or None))
            continue

        start = _pcm_session_start(pcm, wav)
        if start is None:
            finding["audio_source_available"] = True
            failures.append({
                "artifact_requirement": "AUDIO_CLIP",
                "status": "FAILED",
                "reason_code": "SOURCE_METADATA_INVALID",
                "source_available": True,
                "finding_refs": [finding_id],
                "event_refs": [str(candidate.get("event_id") or "")],
                "source_artifact_ids": [wav.id],
            })
            continue
        finding["audio_source_available"] = True
        raw = storage.get_bytes(wav.object_key)
        clip, meta = render_event_audio_clip(
            raw,
            event=candidate,
            source_artifact={"artifact_id": wav.id, "start_time": start},
            finding_id=finding_id,
            analyzer_name="EVIDENCE_V2_ARTIFACT_BINDER",
            analyzer_version="2.0",
            profile_version="preliminary-evidence-v2",
        )
        if not clip:
            failures.append(meta)
            continue
        row = persist_artifact(
            db,
            storage,
            report=report_row,
            artifact_type="ANOMALY_AUDIO_CLIP",
            filename=f"v2_{finding_id}_{candidate.get('event_id')}.wav".replace("/", "_"),
            data=clip,
            content_type="audio/wav",
            metadata={**meta, "source_artifact_id": wav.id, "v2_schema": V2_SCHEMA},
            analyzer_run_id=wav.analyzer_run_id,
            evidence_id=wav.evidence_id,
            finding_ids=[finding_id],
            role="FINDING",
        )
        created.append(row)
        artifacts.append({
            **meta,
            "artifact_id": row.id,
            "type": "ANOMALY_AUDIO_CLIP",
            "filename": row.filename,
            "content_type": row.content_type,
            "size_bytes": row.size_bytes,
            "object_key": row.object_key,
            "sha256": row.sha256,
        })

    v2["artifacts"] = artifacts
    v2["artifact_failures"] = failures
    validation = validate_report_semantics(v2)
    v2["semantic_validation"] = validation
    v2["publishable"] = validation["status"] == "PASS"
    v2["pipeline_status"] = "COMPLETE" if v2["publishable"] else "FAILED_VALIDATION"
    v2["first_page"] = build_first_page(v2)
    return created


def _active_pcm_event(finding: Mapping[str, Any], event_by_id: Mapping[str, Mapping[str, Any]]) -> Mapping[str, Any] | None:
    for ref in finding.get("event_refs") or []:
        event = event_by_id.get(str(ref))
        if not event:
            continue
        if str(event.get("phase") or "").upper() != "ACTIVE_MEDIA":
            continue
        if str(event.get("layer") or "").upper().startswith("PCM_"):
            return event
    return None


def _pcm_wavs(db: Session, pcm_run: AnalyzerRun | None) -> list[Artifact]:
    if pcm_run is None:
        return []
    return list(db.scalars(select(Artifact).where(
        Artifact.analyzer_run_id == pcm_run.id,
        Artifact.type == "PCM_WAV",
    ).order_by(Artifact.created_at.asc())))


def _matching_pcm_wav(wavs: list[Artifact], event: Mapping[str, Any]) -> Artifact | None:
    expected = str(event.get("layer") or "").lower()
    if expected.startswith("pcm_"):
        expected = expected
    for wav in wavs:
        meta = wav.metadata_json or {}
        if str(meta.get("pcm_tap") or "").lower() == expected:
            return wav
    return None


def _pcm_session_start(pcm: Mapping[str, Any], wav: Artifact) -> float | None:
    meta = wav.metadata_json or {}
    tap = str(meta.get("pcm_tap") or "").lower()
    session_index = int(meta.get("session_index") or 0)
    for stream in pcm.get("streams") or []:
        if not isinstance(stream, Mapping):
            continue
        if str((stream.get("tap") or {}).get("name") or "").lower() != tap:
            continue
        sessions = [item for item in stream.get("sessions") or [] if isinstance(item, Mapping)]
        session = next((item for item in sessions if int(item.get("session_index") or 0) == session_index), None)
        if session and session.get("start_time") is not None:
            return float(session["start_time"])
    return None


def _capture_window(packet: Mapping[str, Any], pcm: Mapping[str, Any]) -> dict[str, float] | None:
    points: list[float] = []
    for call in packet.get("calls") or []:
        if not isinstance(call, Mapping):
            continue
        for key in ("start_time", "end_time"):
            if call.get(key) is not None:
                points.append(float(call[key]))
    for stream in packet.get("rtp_streams") or []:
        if not isinstance(stream, Mapping):
            continue
        for key in ("start_time", "end_time"):
            if stream.get(key) is not None:
                points.append(float(stream[key]))
    for stream in pcm.get("streams") or []:
        if not isinstance(stream, Mapping):
            continue
        for session in stream.get("sessions") or []:
            if not isinstance(session, Mapping):
                continue
            for key in ("start_time", "end_time"):
                if session.get(key) is not None:
                    points.append(float(session[key]))
    return {"start": min(points), "end": max(points)} if points else None
