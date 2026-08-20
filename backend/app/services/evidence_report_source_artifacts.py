from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.evidence_report_models import EvidenceFinding, EvidenceReportArtifactLink, PreliminaryEvidenceReport
from app.db.models import AnalyzerRun, Artifact


REPORT_SOURCE_ARTIFACT_TYPES = {
    "AUDIO_CLIP", "PERIODIC_AUDIO_CLIP", "PCM_WAV", "AUDIO_WAV", "PERIODIC_METRICS_JSON",
    "WAVEFORM_JSON", "SPECTROGRAM_JSON",
}
_EVENT_SPECIFIC_TYPES = {"AUDIO_CLIP", "PERIODIC_AUDIO_CLIP", "PERIODIC_METRICS_JSON"}
_RTP_FINDING_TYPES = {"PACKET_LOSS", "BURST_LOSS", "HIGH_DELTA", "PAYLOAD_CHANGE", "ONE_WAY_RTP_MEDIA"}
_SIP_FINDING_TYPES = {"SIP_REGISTRATION_FAILED", "SIP_CALL_FAILED", "SIP_CONFLICTING_FINAL_RESPONSE", "CODEC_NEGOTIATION_MISMATCH"}


def _norm_type(value: str | None) -> str:
    raw = str(value or "").upper()
    return {"SILENCE": "UNEXPECTED_SILENCE"}.get(raw, raw)


def _finding_window(finding: EvidenceFinding) -> tuple[float | None, float | None]:
    start = finding.start_time
    end = finding.end_time if finding.end_time is not None else finding.start_time
    return start, end


def _time_matches(finding: EvidenceFinding, meta: dict, tolerance_seconds: float = 0.25) -> bool:
    event_time = meta.get("event_time")
    if event_time is None:
        return True
    start, end = _finding_window(finding)
    if start is None or end is None:
        return False
    try:
        t = float(event_time)
        return float(start) - tolerance_seconds <= t <= float(end) + tolerance_seconds
    except (TypeError, ValueError):
        return False


def _scope_matches(finding: EvidenceFinding, meta: dict) -> bool:
    scope = finding.scope_json or {}
    matched = False
    stream_id = meta.get("stream_id")
    if stream_id:
        matched = stream_id in {
            scope.get("rtp_stream_id"), scope.get("upstream_rtp_stream_id"), scope.get("downstream_rtp_stream_id")
        }
    pcm_tap = meta.get("pcm_tap")
    if pcm_tap:
        tap_match = scope.get("pcm_tap") == pcm_tap
        if meta.get("session_index") is not None and scope.get("pcm_session_index") is not None:
            tap_match = tap_match and int(scope.get("pcm_session_index")) == int(meta.get("session_index"))
        matched = matched or tap_match
    nested = meta.get("scope") or {}
    if nested:
        nested_match = True
        for key in ("pcm_tap", "pcm_session_index", "upstream_rtp_stream_id", "downstream_rtp_stream_id", "call_id"):
            if nested.get(key) is None or scope.get(key) is None:
                continue
            if str(nested.get(key)) != str(scope.get(key)):
                nested_match = False
                break
        matched = matched or nested_match
    return matched


def _artifact_matches_finding(artifact: Artifact, finding: EvidenceFinding) -> bool:
    meta = artifact.metadata_json or {}
    atype = str(artifact.type or "").upper()
    event_type = _norm_type(meta.get("event_type"))
    finding_type = _norm_type(finding.finding_type)
    scope_match = _scope_matches(finding, meta)

    if atype in _EVENT_SPECIFIC_TYPES:
        # Event clips/metrics must match the Finding semantic type. This prevents
        # a Click clip from being attached to an unrelated periodic/silence
        # Finding merely because both share the same pcm_tap.
        if event_type and event_type != finding_type:
            return False
        if not event_type:
            return False
        if not _time_matches(finding, meta):
            return False
        return scope_match or event_type == finding_type

    # Full audio / waveform source material is allowed to follow the exact
    # stream or PCM session scope, but never event type alone.
    return scope_match


def _artifact_link_role(artifact: Artifact, related: list[str]) -> str:
    if not related:
        return "SOURCE"
    atype = str(artifact.type or "").upper()
    if atype in {"AUDIO_CLIP", "PERIODIC_AUDIO_CLIP"}:
        return "EVENT_AUDIO"
    if atype == "PERIODIC_METRICS_JSON":
        return "EVENT_METRICS"
    if atype in {"PCM_WAV", "AUDIO_WAV"}:
        return "SOURCE_AUDIO"
    if atype in {"WAVEFORM_JSON", "SPECTROGRAM_JSON"}:
        return "SOURCE_VISUAL_DATA"
    return "FINDING"


def link_source_artifacts(db: Session, *, report: PreliminaryEvidenceReport, runs: dict[str, AnalyzerRun]) -> list[Artifact]:
    run_ids = [r.id for r in runs.values() if r]
    if not run_ids:
        return []
    rows = list(db.scalars(select(Artifact).where(
        Artifact.analyzer_run_id.in_(run_ids), Artifact.type.in_(REPORT_SOURCE_ARTIFACT_TYPES)
    ).order_by(Artifact.created_at.asc())))
    findings = list(db.scalars(select(EvidenceFinding).where(
        EvidenceFinding.scope_type == report.scope_type, EvidenceFinding.scope_id == report.scope_id
    )))
    out = []
    for artifact in rows:
        related = sorted({finding.id for finding in findings if _artifact_matches_finding(artifact, finding)})
        exists = db.scalar(select(EvidenceReportArtifactLink).where(
            EvidenceReportArtifactLink.report_id == report.id,
            EvidenceReportArtifactLink.artifact_id == artifact.id,
        ).limit(1))
        if not exists:
            db.add(EvidenceReportArtifactLink(
                report_id=report.id,
                artifact_id=artifact.id,
                finding_ids_json=related,
                role=_artifact_link_role(artifact, related),
            ))
        out.append(artifact)
    db.flush()
    return out


def link_summary_visuals_to_findings(db: Session, *, report: PreliminaryEvidenceReport, artifacts: list[Artifact]) -> None:
    """Bind report-level RTP/SIP summary visuals to the Findings they explain."""
    findings = list(db.scalars(select(EvidenceFinding).where(
        EvidenceFinding.scope_type == report.scope_type, EvidenceFinding.scope_id == report.scope_id
    )))
    for artifact in artifacts:
        atype = str(artifact.type or "").upper()
        if atype not in {"RTP_TIMELINE_PNG", "SIP_CALL_FLOW_PNG"}:
            continue
        related = []
        for finding in findings:
            layer = str((finding.scope_json or {}).get("layer") or "").upper()
            if atype == "RTP_TIMELINE_PNG" and (finding.finding_type in _RTP_FINDING_TYPES or layer == "RTP"):
                related.append(finding.id)
            if atype == "SIP_CALL_FLOW_PNG" and (finding.finding_type in _SIP_FINDING_TYPES or layer == "SIP_SDP"):
                related.append(finding.id)
        link = db.scalar(select(EvidenceReportArtifactLink).where(
            EvidenceReportArtifactLink.report_id == report.id,
            EvidenceReportArtifactLink.artifact_id == artifact.id,
        ).limit(1))
        if link:
            link.finding_ids_json = sorted(set((link.finding_ids_json or []) + related))
            if related and link.role in {None, "SUMMARY"}:
                link.role = "SUMMARY_GRAPH"
    db.flush()


def finding_artifact_refs(db: Session, *, report_id: str, finding_id: str) -> list[dict]:
    links = list(db.scalars(select(EvidenceReportArtifactLink).where(
        EvidenceReportArtifactLink.report_id == report_id
    ).order_by(EvidenceReportArtifactLink.created_at.asc())))
    refs = []
    for link in links:
        if finding_id not in (link.finding_ids_json or []):
            continue
        artifact = db.get(Artifact, link.artifact_id)
        if artifact:
            refs.append({
                "artifact_id": artifact.id,
                "type": artifact.type,
                "filename": artifact.filename,
                "content_type": artifact.content_type,
                "role": link.role,
                "sha256": artifact.sha256,
                "metadata": artifact.metadata_json or {},
            })
    return refs
