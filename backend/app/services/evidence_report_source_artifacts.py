from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.evidence_report_models import EvidenceFinding, EvidenceReportArtifactLink, PreliminaryEvidenceReport
from app.db.models import AnalyzerRun, Artifact

REPORT_SOURCE_ARTIFACT_TYPES={
    "AUDIO_CLIP","PERIODIC_AUDIO_CLIP","PCM_WAV","AUDIO_WAV","PERIODIC_METRICS_JSON",
    "WAVEFORM_JSON","SPECTROGRAM_JSON",
}

_EVENT_ALIASES={
    "SILENCE":"UNEXPECTED_SILENCE",
    "UNEXPECTED_SILENCE":"UNEXPECTED_SILENCE",
    "CLICK_POP":"CLICK_POP",
    "PACKET_LOSS":"PACKET_LOSS",
    "BURST_LOSS":"BURST_LOSS",
    "HIGH_DELTA":"HIGH_DELTA",
    "LOCAL_CAPTURE_PERIODIC_INTERFERENCE":"LOCAL_CAPTURE_PERIODIC_INTERFERENCE",
    "PERIODIC_LOW_FREQUENCY_INTERFERENCE":"PERIODIC_LOW_FREQUENCY_INTERFERENCE",
}
_AUDIO_TYPES={"AUDIO_CLIP","PERIODIC_AUDIO_CLIP"}
_IMAGE_TYPES={"WAVEFORM_PNG","SPECTRUM_PNG","SPECTROGRAM_PNG","RTP_TIMELINE_PNG","SIP_CALL_FLOW_PNG"}


def _event_type(value)->str|None:
    raw=str(value or "").upper()
    return _EVENT_ALIASES.get(raw,raw or None)


def _meta_scope(meta:dict)->dict:
    nested=meta.get("scope") if isinstance(meta.get("scope"),dict) else {}
    return {**nested,**{k:v for k,v in meta.items() if k not in {"scope"}}}


def _time_range(finding:EvidenceFinding)->tuple[float|None,float|None,float|None]:
    start=float(finding.start_time) if finding.start_time is not None else None
    end=float(finding.end_time) if finding.end_time is not None else start
    rep=float(finding.representative_time) if finding.representative_time is not None else start
    return start,end,rep


def _time_matches(meta:dict,finding:EvidenceFinding,*,tolerance_seconds:float=0.35)->bool:
    event_time=meta.get("event_time")
    if event_time is None:
        return True
    try: event=float(event_time)
    except (TypeError,ValueError): return False
    start,end,rep=_time_range(finding)
    candidates=[x for x in (start,end,rep) if x is not None]
    if not candidates:return False
    lo=min(candidates)-tolerance_seconds; hi=max(candidates)+tolerance_seconds
    return lo<=event<=hi or any(abs(event-x)<=tolerance_seconds for x in candidates)


def artifact_matches_finding(artifact:Artifact,finding:EvidenceFinding,*,tolerance_seconds:float=0.35)->bool:
    """Fail-closed Artifact↔Finding binding used by Evidence Card projection."""
    atype=str(artifact.type or "").upper(); meta=_meta_scope(artifact.metadata_json or {}); scope=finding.scope_json or {}
    stream_id=meta.get("stream_id")
    if stream_id:
        valid_streams={scope.get("rtp_stream_id"),scope.get("upstream_rtp_stream_id"),scope.get("downstream_rtp_stream_id")}
        if stream_id not in valid_streams:return False
    pcm_tap=meta.get("pcm_tap")
    if pcm_tap and scope.get("pcm_tap") and pcm_tap!=scope.get("pcm_tap"):return False
    session_index=meta.get("session_index")
    finding_session=scope.get("pcm_session_index")
    if session_index is not None and finding_session is not None and int(session_index)!=int(finding_session):return False

    artifact_event=_event_type(meta.get("event_type")); finding_event=_event_type(finding.finding_type)
    if atype in _AUDIO_TYPES:
        if artifact_event and artifact_event!=finding_event:return False
        if not artifact_event and finding_event not in {"LOCAL_CAPTURE_PERIODIC_INTERFERENCE","PERIODIC_LOW_FREQUENCY_INTERFERENCE"}:return False
        if not _time_matches(meta,finding,tolerance_seconds=tolerance_seconds) and atype!="PERIODIC_AUDIO_CLIP":return False
        return bool(stream_id or pcm_tap or artifact_event)

    if atype=="PERIODIC_METRICS_JSON":
        return artifact_event==finding_event and _time_matches(meta,finding,tolerance_seconds=2.0)
    if atype in {"WAVEFORM_JSON","SPECTROGRAM_JSON","PCM_WAV","AUDIO_WAV"}:
        if pcm_tap:return pcm_tap==scope.get("pcm_tap") and (session_index is None or finding_session is None or int(session_index)==int(finding_session))
        if stream_id:return stream_id in {scope.get("rtp_stream_id"),scope.get("upstream_rtp_stream_id"),scope.get("downstream_rtp_stream_id")}
        return False
    return False


def link_source_artifacts(db:Session,*,report:PreliminaryEvidenceReport,runs:dict[str,AnalyzerRun]) -> list[Artifact]:
    run_ids=[r.id for r in runs.values() if r]
    if not run_ids:return []
    rows=list(db.scalars(select(Artifact).where(Artifact.analyzer_run_id.in_(run_ids),Artifact.type.in_(REPORT_SOURCE_ARTIFACT_TYPES)).order_by(Artifact.created_at.asc())))
    findings=list(db.scalars(select(EvidenceFinding).where(
        EvidenceFinding.scope_type==report.scope_type,
        EvidenceFinding.scope_id==report.scope_id,
        EvidenceFinding.last_seen_report_version==report.version,
    )))
    out=[]
    for artifact in rows:
        related=[f.id for f in findings if artifact_matches_finding(artifact,f)]
        exists=db.scalar(select(EvidenceReportArtifactLink).where(EvidenceReportArtifactLink.report_id==report.id,EvidenceReportArtifactLink.artifact_id==artifact.id).limit(1))
        if not exists:
            db.add(EvidenceReportArtifactLink(report_id=report.id,artifact_id=artifact.id,finding_ids_json=sorted(set(related)),
                                              role="FINDING" if related else "SOURCE"))
        out.append(artifact)
    db.flush();return out


def _human_visual_ready_meta(meta:dict)->bool:
    if str(meta.get("renderer_family") or "").upper()!="HUMAN":return False
    if not bool(meta.get("annotation_complete")):return False
    explanation=meta.get("human_explanation")
    if not isinstance(explanation,dict):return False
    required=("what_to_look_at","meaning","evidence_boundary","plain_language_summary")
    if any(not str(explanation.get(key) or "").strip() for key in required):return False
    if str(explanation.get("diagnostic_authority") or "NONE").upper()!="NONE":return False
    return True


def _is_human_visual(artifact:Artifact)->bool:
    return _human_visual_ready_meta(artifact.metadata_json or {})


def _prefer_human_visuals(artifacts:list[Artifact])->list[Artifact]:
    """Prefer only presentation-ready Human V2 images; otherwise keep Machine."""
    human_types={str(a.type or "").upper() for a in artifacts if str(a.type or "").upper() in _IMAGE_TYPES and _is_human_visual(a)}
    if not human_types:return artifacts
    out=[]
    for artifact in artifacts:
        atype=str(artifact.type or "").upper()
        if atype in human_types and atype in _IMAGE_TYPES and not _is_human_visual(artifact):
            continue
        out.append(artifact)
    return out


def _human_caption(meta:dict)->str|None:
    if not _human_visual_ready_meta(meta):return None
    explanation=meta.get("human_explanation") or {}
    summary=str(explanation.get("plain_language_summary") or "").strip()
    visual_kind=str(meta.get("visual_kind") or meta.get("kind") or "Human Visual")
    return f"{visual_kind}｜{summary}" if summary else visual_kind


def _projection_metadata(artifact:Artifact)->dict:
    meta=dict(artifact.metadata_json or {})
    if _is_human_visual(artifact):
        annotation=dict(meta.get("annotation_contract") or {})
        caption=_human_caption(meta)
        if caption:annotation["caption"]=caption
        annotation["human_explanation_rendered"]="STRUCTURED_POST_IMAGE_V2"
        meta["annotation_contract"]=annotation
        meta["human_visual_ready"]=True
    return meta


def finding_artifact_refs(db:Session,*,report_id:str,finding_id:str) -> list[dict]:
    links=list(db.scalars(select(EvidenceReportArtifactLink).where(EvidenceReportArtifactLink.report_id==report_id).order_by(EvidenceReportArtifactLink.created_at.asc())))
    artifacts=[];roles={}
    for link in links:
        if finding_id not in (link.finding_ids_json or []):continue
        artifact=db.get(Artifact,link.artifact_id)
        if artifact:artifacts.append(artifact);roles[artifact.id]=link.role
    artifacts=_prefer_human_visuals(artifacts)
    refs=[]
    for artifact in artifacts:
        refs.append({
            "artifact_id":artifact.id,"type":artifact.type,"filename":artifact.filename,"content_type":artifact.content_type,
            "role":roles.get(artifact.id),"sha256":artifact.sha256,"metadata":_projection_metadata(artifact),
        })
    return refs
