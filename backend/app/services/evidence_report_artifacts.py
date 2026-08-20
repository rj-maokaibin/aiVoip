from __future__ import annotations

from sqlalchemy import select

from app.contracts.evidence_report import EvidenceReportArtifactType
from app.db.evidence_report_models import EvidenceFinding
from app.reports.evidence_visuals import (
    render_rtp_timeline_png,
    render_sip_call_flow_png,
    render_spectrum_png,
    render_spectrogram_png,
    render_waveform_png,
    visual_metadata,
)
from .evidence_report_artifacts_core import *  # noqa: F401,F403
from .evidence_report_artifacts_core import (
    _media_json_artifacts,
    generate_visual_artifacts as _core_generate_visual_artifacts,
    persist_artifact,
)


_PERIODIC_TYPES = {"PERIODIC_LOW_FREQUENCY_INTERFERENCE", "LOCAL_CAPTURE_PERIODIC_INTERFERENCE", "PERIODIC_INTERFERENCE_PATH_COMPARISON"}
_RTP_TYPES = {"PACKET_LOSS", "BURST_LOSS", "HIGH_DELTA", "PAYLOAD_CHANGE", "ONE_WAY_RTP_MEDIA"}
_SIP_TYPES = {"SIP_REGISTRATION_FAILED", "SIP_CALL_FAILED", "SIP_CONFLICTING_FINAL_RESPONSE", "CODEC_NEGOTIATION_MISMATCH"}
_PCM_EVENT_TYPES = {"CLICK_POP", "UNEXPECTED_SILENCE"}


def _semantic_title(ftype: str) -> str:
    return {
        "CLICK_POP": "PCM CLICK / POP EVENT",
        "UNEXPECTED_SILENCE": "PCM SILENCE MISMATCH",
        "LOCAL_CAPTURE_PERIODIC_INTERFERENCE": "PCM RX PERIODIC INTERFERENCE",
        "PERIODIC_LOW_FREQUENCY_INTERFERENCE": "PCM PERIODIC LOW FREQUENCY",
        "PERIODIC_INTERFERENCE_PATH_COMPARISON": "CROSS LAYER PERIODIC COMPARISON",
        "HIGH_DELTA": "RTP HIGH DELTA INCIDENT",
        "PACKET_LOSS": "RTP PACKET LOSS INCIDENT",
        "BURST_LOSS": "RTP BURST LOSS INCIDENT",
        "ONE_WAY_RTP_MEDIA": "RTP ONE WAY MEDIA",
        "SIP_CALL_FAILED": "SIP CALL FAILURE",
        "SIP_REGISTRATION_FAILED": "SIP REGISTRATION FAILURE",
        "CODEC_NEGOTIATION_MISMATCH": "SIP SDP CODEC MISMATCH",
    }.get(ftype, ftype.replace("_", " "))


def _semantic_context(finding: EvidenceFinding) -> dict:
    scope = finding.scope_json or {}
    call = scope.get("call_id") or finding.call_id or "N/A"
    start = finding.start_time
    return {
        "title": _semantic_title(str(finding.finding_type or "FINDING")),
        "subtitle": f"FINDING {finding.id[:8]} | CALL {str(call)[:24]} | T {start if start is not None else 'N/A'}",
    }


def _relative_window(finding: EvidenceFinding, source_meta: dict) -> tuple[float | None, float | None]:
    base = source_meta.get("start_time")
    if base is None or finding.start_time is None:
        return None, None
    try:
        start = max(0.0, float(finding.start_time) - float(base))
        end_abs = finding.end_time if finding.end_time is not None else finding.start_time
        end = max(start, float(end_abs) - float(base))
        return start, end
    except (TypeError, ValueError):
        return None, None


def _source_json_for_finding(media_json: list[tuple], finding: EvidenceFinding, source_type: str):
    scope = finding.scope_json or {}
    for source, data in media_json:
        if source.type != source_type:
            continue
        meta = source.metadata_json or {}
        if scope.get("rtp_stream_id") and meta.get("stream_id") == scope.get("rtp_stream_id"):
            return source, data
        if scope.get("pcm_tap") and meta.get("pcm_tap") == scope.get("pcm_tap"):
            if scope.get("pcm_session_index") is None or meta.get("session_index") == scope.get("pcm_session_index"):
                return source, data
    return None


def _pcm_session(pcm: dict, finding: EvidenceFinding) -> dict | None:
    scope = finding.scope_json or {}
    for stream in pcm.get("streams", []) or []:
        if (stream.get("tap") or {}).get("name") != scope.get("pcm_tap"):
            continue
        for session in stream.get("sessions", []) or []:
            if scope.get("pcm_session_index") is None or session.get("session_index") == scope.get("pcm_session_index"):
                return session
    return None


def _packet_stream(packet: dict, finding: EvidenceFinding) -> dict | None:
    stream_id = (finding.scope_json or {}).get("rtp_stream_id")
    if not stream_id:
        return None
    return next((s for s in packet.get("rtp_streams", []) or [] if s.get("stream_id") == stream_id), None)


def _sip_calls(packet: dict, finding: EvidenceFinding) -> list[dict]:
    call_id = (finding.scope_json or {}).get("call_id")
    calls = packet.get("calls", []) or []
    if not call_id:
        return calls[:1]
    matched = [c for c in calls if c.get("call_id") == call_id]
    return matched or calls[:1]


def _persist_semantic_graph(db, storage, *, report, finding: EvidenceFinding, artifact_type: str,
                            filename: str, data: bytes, source_meta: dict, role: str):
    return persist_artifact(
        db, storage, report=report, artifact_type=artifact_type, filename=filename,
        data=data, content_type="image/png",
        metadata=visual_metadata(
            artifact_type,
            source={**source_meta, "finding_id": finding.id, "finding_type": finding.finding_type},
            window={"start": finding.start_time, "end": finding.end_time},
            annotations={"semantic_role": role, "finding_id": finding.id},
        ),
        analyzer_run_id=source_meta.get("analyzer_run_id"),
        evidence_id=source_meta.get("evidence_id"),
        finding_ids=[finding.id],
        role=role,
    )


def generate_visual_artifacts(db, storage, *, report, results, runs):
    """Generate generic summary visuals plus Finding-bound semantic evidence graphs."""
    created = list(_core_generate_visual_artifacts(db, storage, report=report, results=results, runs=runs))
    findings = list(db.scalars(select(EvidenceFinding).where(
        EvidenceFinding.scope_type == report.scope_type,
        EvidenceFinding.scope_id == report.scope_id,
    )))
    media_run = runs.get("media_intelligence")
    media_json = _media_json_artifacts(db, storage, media_run)
    packet = results.get("packet_intelligence") or {}
    pcm = results.get("pcm_intelligence") or {}
    packet_run = runs.get("packet_intelligence")
    pcm_run = runs.get("pcm_intelligence")
    semantic_count = 0

    for finding in findings:
        if str(finding.severity or "INFO").upper() not in {"MEDIUM", "HIGH", "CRITICAL"}:
            continue
        if semantic_count >= 24:
            break
        ftype = str(finding.finding_type or "")
        context = _semantic_context(finding)

        if ftype in _RTP_TYPES:
            stream = _packet_stream(packet, finding)
            if stream:
                data = render_rtp_timeline_png([stream], context=context)
                created.append(_persist_semantic_graph(
                    db, storage, report=report, finding=finding,
                    artifact_type=EvidenceReportArtifactType.RTP_TIMELINE_PNG.value,
                    filename=f"finding_{finding.id[:8]}_rtp_timeline.png", data=data,
                    source_meta={"analyzer_run_id": packet_run.id if packet_run else None, "rtp_stream_id": stream.get("stream_id")},
                    role="PRIMARY_GRAPH",
                )); semantic_count += 1
            continue

        if ftype in _SIP_TYPES:
            calls = _sip_calls(packet, finding)
            if calls:
                data = render_sip_call_flow_png(calls, context=context)
                created.append(_persist_semantic_graph(
                    db, storage, report=report, finding=finding,
                    artifact_type=EvidenceReportArtifactType.SIP_CALL_FLOW_PNG.value,
                    filename=f"finding_{finding.id[:8]}_sip_flow.png", data=data,
                    source_meta={"analyzer_run_id": packet_run.id if packet_run else None},
                    role="PRIMARY_GRAPH",
                )); semantic_count += 1
            continue

        if ftype in _PERIODIC_TYPES:
            session = _pcm_session(pcm, finding)
            spectral = (session or {}).get("spectral") or {}
            if spectral:
                data = render_spectrum_png(spectral, context=context)
                created.append(_persist_semantic_graph(
                    db, storage, report=report, finding=finding,
                    artifact_type=EvidenceReportArtifactType.SPECTRUM_PNG.value,
                    filename=f"finding_{finding.id[:8]}_spectrum.png", data=data,
                    source_meta={"analyzer_run_id": pcm_run.id if pcm_run else None, "pcm_tap": (finding.scope_json or {}).get("pcm_tap"), "session_index": (finding.scope_json or {}).get("pcm_session_index")},
                    role="PRIMARY_GRAPH",
                )); semantic_count += 1
            source = _source_json_for_finding(media_json, finding, "WAVEFORM_JSON")
            if source and semantic_count < 24:
                art, wave = source; rel_start, rel_end = _relative_window(finding, art.metadata_json or {})
                data = render_waveform_png(wave, anomaly_start=rel_start, anomaly_end=rel_end, context=context)
                created.append(_persist_semantic_graph(
                    db, storage, report=report, finding=finding,
                    artifact_type=EvidenceReportArtifactType.WAVEFORM_PNG.value,
                    filename=f"finding_{finding.id[:8]}_waveform.png", data=data,
                    source_meta={"analyzer_run_id": art.analyzer_run_id, "evidence_id": art.evidence_id, "source_artifact_id": art.id},
                    role="SUPPORTING_GRAPH",
                )); semantic_count += 1
            continue

        if ftype in _PCM_EVENT_TYPES:
            source = _source_json_for_finding(media_json, finding, "WAVEFORM_JSON")
            if source:
                art, wave = source; rel_start, rel_end = _relative_window(finding, art.metadata_json or {})
                data = render_waveform_png(wave, anomaly_start=rel_start, anomaly_end=rel_end, context=context)
                created.append(_persist_semantic_graph(
                    db, storage, report=report, finding=finding,
                    artifact_type=EvidenceReportArtifactType.WAVEFORM_PNG.value,
                    filename=f"finding_{finding.id[:8]}_waveform.png", data=data,
                    source_meta={"analyzer_run_id": art.analyzer_run_id, "evidence_id": art.evidence_id, "source_artifact_id": art.id},
                    role="PRIMARY_GRAPH",
                )); semantic_count += 1
            source = _source_json_for_finding(media_json, finding, "SPECTROGRAM_JSON")
            if source and semantic_count < 24:
                art, spec = source; rel_start, rel_end = _relative_window(finding, art.metadata_json or {})
                data = render_spectrogram_png(spec, anomaly_start=rel_start, anomaly_end=rel_end, context=context)
                created.append(_persist_semantic_graph(
                    db, storage, report=report, finding=finding,
                    artifact_type=EvidenceReportArtifactType.SPECTROGRAM_PNG.value,
                    filename=f"finding_{finding.id[:8]}_spectrogram.png", data=data,
                    source_meta={"analyzer_run_id": art.analyzer_run_id, "evidence_id": art.evidence_id, "source_artifact_id": art.id},
                    role="SUPPORTING_GRAPH",
                )); semantic_count += 1

    return created
