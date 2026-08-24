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
    if event_time is None:return True
    try:event=float(event_time)
    except (TypeError,ValueError):return False
    start,end,rep=_time_range(finding);candidates=[x for x in (start,end,rep) if x is not None]
    if not candidates:return False
    lo=min(candidates)-tolerance_seconds;hi=max(candidates)+tolerance_seconds
    return lo<=event<=hi or any(abs(event-x)<=tolerance_seconds for x in candidates)


def artifact_matches_finding(artifact:Artifact,finding:EvidenceFinding,*,tolerance_seconds:float=0.35)->bool:
    atype=str(artifact.type or "").upper();meta=_meta_scope(artifact.metadata_json or {});scope=finding.scope_json or {}
    stream_id=meta.get("stream_id")
    if stream_id:
        valid_streams={scope.get("rtp_stream_id"),scope.get("upstream_rtp_stream_id"),scope.get("downstream_rtp_stream_id")}
        if stream_id not in valid_streams:return False
    pcm_tap=meta.get("pcm_tap")
    if pcm_tap and scope.get("pcm_tap") and pcm_tap!=scope.get("pcm_tap"):return False
    session_index=meta.get("session_index");finding_session=scope.get("pcm_session_index")
    if session_index is not None and finding_session is not None and int(session_index)!=int(finding_session):return False
    artifact_event=_event_type(meta.get("event_type"));finding_event=_event_type(finding.finding_type)
    if atype in _AUDIO_TYPES:
        if artifact_event and artifact_event!=finding_event:return False
        if not artifact_event and finding_event not in {"LOCAL_CAPTURE_PERIODIC_INTERFERENCE","PERIODIC_LOW_FREQUENCY_INTERFERENCE"}:return False
        if not _time_matches(meta,finding,tolerance_seconds=tolerance_seconds) and atype!="PERIODIC_AUDIO_CLIP":return False
        return bool(stream_id or pcm_tap or artifact_event)
    if atype=="PERIODIC_METRICS_JSON":return artifact_event==finding_event and _time_matches(meta,finding,tolerance_seconds=2.0)
    if atype in {"WAVEFORM_JSON","SPECTROGRAM_JSON","PCM_WAV","AUDIO_WAV"}:
        if pcm_tap:return pcm_tap==scope.get("pcm_tap") and (session_index is None or finding_session is None or int(session_index)==int(finding_session))
        if stream_id:return stream_id in {scope.get("rtp_stream_id"),scope.get("upstream_rtp_stream_id"),scope.get("downstream_rtp_stream_id")}
        return False
    return False


def link_source_artifacts(db:Session,*,report:PreliminaryEvidenceReport,runs:dict[str,AnalyzerRun])->list[Artifact]:
    run_ids=[r.id for r in runs.values() if r]
    if not run_ids:return []
    rows=list(db.scalars(select(Artifact).where(Artifact.analyzer_run_id.in_(run_ids),Artifact.type.in_(REPORT_SOURCE_ARTIFACT_TYPES)).order_by(Artifact.created_at.asc())))
    findings=list(db.scalars(select(EvidenceFinding).where(EvidenceFinding.scope_type==report.scope_type,EvidenceFinding.scope_id==report.scope_id,EvidenceFinding.last_seen_report_version==report.version)))
    out=[]
    for artifact in rows:
        related=[f.id for f in findings if artifact_matches_finding(artifact,f)]
        exists=db.scalar(select(EvidenceReportArtifactLink).where(EvidenceReportArtifactLink.report_id==report.id,EvidenceReportArtifactLink.artifact_id==artifact.id).limit(1))
        if not exists:db.add(EvidenceReportArtifactLink(report_id=report.id,artifact_id=artifact.id,finding_ids_json=sorted(set(related)),role="FINDING" if related else "SOURCE"))
        out.append(artifact)
    db.flush();return out


def _human_visual_ready_meta(meta:dict)->bool:
    if str(meta.get("renderer_family") or "").upper()!="HUMAN":return False
    if not bool(meta.get("annotation_complete")):return False
    explanation=meta.get("human_explanation")
    if not isinstance(explanation,dict):return False
    required=("what_to_look_at","meaning","evidence_boundary","plain_language_summary")
    if any(not str(explanation.get(key) or "").strip() for key in required):return False
    return str(explanation.get("diagnostic_authority") or "NONE").upper()=="NONE"


def _is_human_visual(artifact:Artifact)->bool:return _human_visual_ready_meta(artifact.metadata_json or {})


def _declared_human_image(artifact:Artifact)->bool:
    return (
        str(artifact.type or "").upper() in _IMAGE_TYPES
        and str((artifact.metadata_json or {}).get("renderer_family") or "").upper()=="HUMAN"
    )


def _priority(artifact:Artifact)->int:
    try:return int((artifact.metadata_json or {}).get("presentation_priority") or 0)
    except (TypeError,ValueError):return 0


def _human_group_key(artifact:Artifact)->tuple[str,str,str]:
    meta=artifact.metadata_json or {}
    return (
        str(artifact.type or "").upper(),
        str(meta.get("visual_kind") or meta.get("kind") or artifact.type or "").upper(),
        str(meta.get("visual_instance_id") or ""),
    )


def _prefer_human_visuals(artifacts:list[Artifact])->list[Artifact]:
    """Projection-only Human preference; complete DB links and Bundle remain untouched.

    Declared Human images that do not satisfy the full readiness contract are
    removed from the presentation projection. This is fail-closed: they remain in
    DB/Manifest/Bundle for audit, while the report falls back to Machine visuals.
    """
    candidates=[
        a for a in artifacts
        if not (_declared_human_image(a) and not _is_human_visual(a))
    ]
    ready=[a for a in candidates if str(a.type or "").upper() in _IMAGE_TYPES and _is_human_visual(a)]
    if not ready:return candidates
    winners={}
    for artifact in ready:
        key=_human_group_key(artifact)
        current=winners.get(key)
        if current is None or (_priority(artifact),str(artifact.filename or ""))>(_priority(current),str(current.filename or "")):
            winners[key]=artifact
    winner_ids={a.id for a in winners.values()}
    human_types={str(a.type or "").upper() for a in winners.values()}
    out=[]
    for artifact in candidates:
        atype=str(artifact.type or "").upper()
        if atype not in _IMAGE_TYPES:
            out.append(artifact);continue
        if artifact.id in winner_ids:
            out.append(artifact);continue
        if _is_human_visual(artifact):
            continue
        if atype in human_types:
            continue
        out.append(artifact)
    return out


def _human_caption(meta:dict)->str|None:
    if not _human_visual_ready_meta(meta):return None
    explanation=meta.get("human_explanation") or {};summary=str(explanation.get("plain_language_summary") or "").strip()
    visual_kind=str(meta.get("visual_kind") or meta.get("kind") or "Human Visual")
    return f"{visual_kind}｜{summary}" if summary else visual_kind


def _projection_metadata(artifact:Artifact)->dict:
    meta=dict(artifact.metadata_json or {})
    if _is_human_visual(artifact):
        annotation=dict(meta.get("annotation_contract") or {})
        caption=_human_caption(meta)
        if caption:annotation["caption"]=caption
        annotation["human_explanation_rendered"]="STRUCTURED_POST_IMAGE_V2"
        annotation["human_explanation"]=dict(meta.get("human_explanation") or {})
        meta["annotation_contract"]=annotation;meta["human_visual_ready"]=True
    return meta


def _presentation_images(artifacts:list[Artifact],limit:int=3)->set[str]:
    images=[a for a in artifacts if str(a.type or "").upper() in _IMAGE_TYPES]
    if not any(_is_human_visual(a) for a in images):
        return {a.id for a in images}
    images.sort(key=lambda a:(-_priority(a),str(a.filename or ""),a.id))
    return {a.id for a in images[:limit]}


def finding_artifact_refs(db:Session,*,report_id:str,finding_id:str)->list[dict]:
    links=list(db.scalars(select(EvidenceReportArtifactLink).where(EvidenceReportArtifactLink.report_id==report_id).order_by(EvidenceReportArtifactLink.created_at.asc())))
    artifacts=[];roles={}
    for link in links:
        if finding_id not in (link.finding_ids_json or []):continue
        artifact=db.get(Artifact,link.artifact_id)
        if artifact:artifacts.append(artifact);roles[artifact.id]=link.role
    artifacts=_prefer_human_visuals(artifacts)
    visible_image_ids=_presentation_images(artifacts,limit=3)
    projected=[a for a in artifacts if str(a.type or "").upper() not in _IMAGE_TYPES or a.id in visible_image_ids]
    return [{"artifact_id":a.id,"type":a.type,"filename":a.filename,"content_type":a.content_type,"role":roles.get(a.id),"sha256":a.sha256,"metadata":_projection_metadata(a)} for a in projected]
