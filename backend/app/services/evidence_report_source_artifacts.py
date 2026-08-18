from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.evidence_report_models import EvidenceFinding, EvidenceReportArtifactLink, PreliminaryEvidenceReport
from app.db.models import AnalyzerRun, Artifact

REPORT_SOURCE_ARTIFACT_TYPES={
    "AUDIO_CLIP","PERIODIC_AUDIO_CLIP","PCM_WAV","AUDIO_WAV","PERIODIC_METRICS_JSON",
    "WAVEFORM_JSON","SPECTROGRAM_JSON",
}


def link_source_artifacts(db:Session,*,report:PreliminaryEvidenceReport,runs:dict[str,AnalyzerRun]) -> list[Artifact]:
    run_ids=[r.id for r in runs.values() if r]
    if not run_ids: return []
    rows=list(db.scalars(select(Artifact).where(Artifact.analyzer_run_id.in_(run_ids),Artifact.type.in_(REPORT_SOURCE_ARTIFACT_TYPES)).order_by(Artifact.created_at.asc())))
    findings=list(db.scalars(select(EvidenceFinding).where(EvidenceFinding.scope_type==report.scope_type,EvidenceFinding.scope_id==report.scope_id)))
    out=[]
    for artifact in rows:
        meta=artifact.metadata_json or {}; related=[]
        for finding in findings:
            scope=finding.scope_json or {}
            if meta.get("stream_id") and (scope.get("rtp_stream_id")==meta.get("stream_id") or scope.get("upstream_rtp_stream_id")==meta.get("stream_id") or scope.get("downstream_rtp_stream_id")==meta.get("stream_id")):
                related.append(finding.id)
            if meta.get("pcm_tap") and scope.get("pcm_tap")==meta.get("pcm_tap"):
                related.append(finding.id)
            if meta.get("event_type") and finding.finding_type==meta.get("event_type"):
                related.append(finding.id)
        exists=db.scalar(select(EvidenceReportArtifactLink).where(EvidenceReportArtifactLink.report_id==report.id,EvidenceReportArtifactLink.artifact_id==artifact.id).limit(1))
        if not exists:
            db.add(EvidenceReportArtifactLink(report_id=report.id,artifact_id=artifact.id,finding_ids_json=sorted(set(related)),
                                              role="FINDING" if related else "SOURCE"))
        out.append(artifact)
    db.flush(); return out


def finding_artifact_refs(db:Session,*,report_id:str,finding_id:str) -> list[dict]:
    links=list(db.scalars(select(EvidenceReportArtifactLink).where(EvidenceReportArtifactLink.report_id==report_id).order_by(EvidenceReportArtifactLink.created_at.asc())))
    refs=[]
    for link in links:
        if finding_id not in (link.finding_ids_json or []):
            continue
        artifact=db.get(Artifact,link.artifact_id)
        if artifact:
            refs.append({"artifact_id":artifact.id,"type":artifact.type,"filename":artifact.filename,"content_type":artifact.content_type,"role":link.role})
    return refs
