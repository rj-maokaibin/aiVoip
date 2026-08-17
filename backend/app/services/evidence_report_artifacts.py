from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.contracts.evidence_report import EvidenceReportArtifactType, EvidenceReportScope
from app.db.evidence_report_models import EvidenceFinding, EvidenceReportArtifactLink, PreliminaryEvidenceReport
from app.db.models import AnalyzerRun, Artifact, Evidence
from app.reports.evidence_visuals import (
    render_rtp_timeline_png, render_sip_call_flow_png, render_spectrum_png,
    render_spectrogram_png, render_waveform_png, visual_metadata,
)
from app.services.audit import audit


def utcnow() -> datetime: return datetime.now(timezone.utc)


def persist_artifact(db: Session, storage, *, report: PreliminaryEvidenceReport, artifact_type: str,
                     filename: str, data: bytes, content_type: str, metadata: dict,
                     evidence_id: str | None = None, analyzer_run_id: str | None = None,
                     finding_ids: list[str] | None = None, role: str | None = None) -> Artifact:
    object_key=f"cases/{report.case_id}/reports/evidence/{report.id}/{filename}"
    storage.put_bytes(object_key,data,content_type)
    row=Artifact(case_id=report.case_id,analyzer_run_id=analyzer_run_id,evidence_id=evidence_id,
                 type=artifact_type,filename=filename,object_key=object_key,content_type=content_type,
                 size_bytes=len(data),sha256=hashlib.sha256(data).hexdigest(),metadata_json={
                     **metadata,"report_id":report.id,"report_version":report.version,
                     "scope_type":report.scope_type,"scope_id":report.scope_id,"session_id":report.session_id,"call_id":report.call_id,
                     "finding_ids":finding_ids or [],
                 })
    db.add(row); db.flush()
    db.add(EvidenceReportArtifactLink(report_id=report.id,artifact_id=row.id,finding_ids_json=finding_ids or [],role=role)); db.flush()
    return row


def _media_json_artifacts(db: Session, storage, media_run: AnalyzerRun | None) -> list[tuple[Artifact,dict]]:
    if not media_run: return []
    rows=list(db.scalars(select(Artifact).where(Artifact.analyzer_run_id==media_run.id,Artifact.type.in_(["WAVEFORM_JSON","SPECTROGRAM_JSON"])).order_by(Artifact.created_at.asc())))
    out=[]
    for row in rows:
        try: out.append((row,json.loads(storage.get_bytes(row.object_key).decode("utf-8"))))
        except Exception: continue
    return out


def generate_visual_artifacts(db: Session, storage, *, report: PreliminaryEvidenceReport,
                              results: dict[str,dict|None], runs: dict[str,AnalyzerRun]) -> list[Artifact]:
    created=[]; packet=results.get("packet_intelligence") or {}; pcm=results.get("pcm_intelligence") or {}
    packet_run=runs.get("packet_intelligence"); pcm_run=runs.get("pcm_intelligence"); media_run=runs.get("media_intelligence")
    if packet.get("rtp_streams"):
        created.append(persist_artifact(db,storage,report=report,artifact_type=EvidenceReportArtifactType.RTP_TIMELINE_PNG.value,
            filename="rtp_timeline.png",data=render_rtp_timeline_png(packet.get("rtp_streams") or []),content_type="image/png",
            metadata=visual_metadata("RTP_TIMELINE",source={"analyzer_run_id":packet_run.id if packet_run else None}),
            analyzer_run_id=packet_run.id if packet_run else None,role="SUMMARY"))
    if packet.get("calls"):
        created.append(persist_artifact(db,storage,report=report,artifact_type=EvidenceReportArtifactType.SIP_CALL_FLOW_PNG.value,
            filename="sip_call_flow.png",data=render_sip_call_flow_png(packet.get("calls") or []),content_type="image/png",
            metadata=visual_metadata("SIP_CALL_FLOW",source={"analyzer_run_id":packet_run.id if packet_run else None}),
            analyzer_run_id=packet_run.id if packet_run else None,role="SUMMARY"))
    spectra=0
    finding_rows=list(db.scalars(select(EvidenceFinding).where(EvidenceFinding.scope_type==report.scope_type,EvidenceFinding.scope_id==report.scope_id)))
    for stream in pcm.get("streams",[]) or []:
        tap=(stream.get("tap") or {}).get("name") or "pcm"
        for sess in stream.get("sessions",[]) or []:
            hum=sess.get("hum") or {}; spectral=sess.get("spectral") or {}
            if str(hum.get("level") or "LOW").upper() not in {"MEDIUM","HIGH"} or not spectral or spectra>=4: continue
            related=[f.id for f in finding_rows if f.finding_type in {"PERIODIC_LOW_FREQUENCY_INTERFERENCE","LOCAL_CAPTURE_PERIODIC_INTERFERENCE"} and (f.scope_json or {}).get("pcm_tap")==tap]
            created.append(persist_artifact(db,storage,report=report,artifact_type=EvidenceReportArtifactType.SPECTRUM_PNG.value,
                filename=f"spectrum_{tap}_{sess.get('session_index',0)}.png",data=render_spectrum_png(spectral),content_type="image/png",
                metadata=visual_metadata("SPECTRUM",source={"pcm_tap":tap,"session_index":sess.get("session_index")},window={"start":sess.get("start_time"),"end":sess.get("end_time")}),
                analyzer_run_id=pcm_run.id if pcm_run else None,finding_ids=related,role="FINDING")); spectra+=1
    for source,data_json in _media_json_artifacts(db,storage,media_run)[:12]:
        meta=source.metadata_json or {}; related=[]
        for finding in finding_rows:
            scope=finding.scope_json or {}
            if meta.get("stream_id") and scope.get("rtp_stream_id")==meta.get("stream_id"): related.append(finding.id)
            if meta.get("pcm_tap") and scope.get("pcm_tap")==meta.get("pcm_tap"): related.append(finding.id)
        if source.type=="WAVEFORM_JSON":
            data=render_waveform_png(data_json); atype=EvidenceReportArtifactType.WAVEFORM_PNG.value; suffix="waveform"
        else:
            data=render_spectrogram_png(data_json); atype=EvidenceReportArtifactType.SPECTROGRAM_PNG.value; suffix="spectrogram"
        created.append(persist_artifact(db,storage,report=report,artifact_type=atype,filename=f"{source.id}_{suffix}.png",data=data,content_type="image/png",
            metadata=visual_metadata(suffix.upper(),source={"source_artifact_id":source.id,**meta}),analyzer_run_id=source.analyzer_run_id,
            evidence_id=source.evidence_id,finding_ids=sorted(set(related)),role="FINDING" if related else "DETAIL"))
    return created


def build_manifest(report: PreliminaryEvidenceReport, artifacts: list[Artifact]) -> dict:
    return {"schema_version":"evidence-bundle-manifest-v1","report_id":report.id,"report_version":report.version,
            "scope":{"type":report.scope_type,"id":report.scope_id},"created_at":utcnow().isoformat(),"artifacts":[{
                "artifact_id":a.id,"type":a.type,"filename":a.filename,"sha256":a.sha256,"size_bytes":a.size_bytes,"content_type":a.content_type,
                "object_key":a.object_key,"analyzer_run_id":a.analyzer_run_id,"evidence_id":a.evidence_id,"metadata":a.metadata_json or {},
            } for a in artifacts]}


def report_artifacts(db: Session, report_id: str) -> list[Artifact]:
    links=list(db.scalars(select(EvidenceReportArtifactLink).where(EvidenceReportArtifactLink.report_id==report_id).order_by(EvidenceReportArtifactLink.created_at.asc())))
    return [a for a in (db.get(Artifact,l.artifact_id) for l in links) if a]


_FULL_AUDIO_TYPES={"PCM_WAV","RTP_WAV","AUDIO_WAV"}
_CLIP_TYPES={"AUDIO_CLIP","PERIODIC_AUDIO_CLIP"}
_IMAGE_TYPES={"WAVEFORM_PNG","SPECTRUM_PNG","SPECTROGRAM_PNG","RTP_TIMELINE_PNG","SIP_CALL_FLOW_PNG"}
_REPORT_TYPES={"PRELIMINARY_REPORT_HTML","PRELIMINARY_REPORT_JSON","MANIFEST_JSON"}


def _artifact_allowed_for_profile(artifact:Artifact,profile:str)->bool:
    if artifact.type==EvidenceReportArtifactType.EVIDENCE_BUNDLE.value:
        return False
    if profile=="INTERNAL_FULL":
        return True
    # SHARE_SAFE intentionally excludes full WAV artifacts. Abnormal clips,
    # deterministic images and structured analysis remain available.
    return artifact.type not in _FULL_AUDIO_TYPES


def _artifact_bundle_path(artifact:Artifact)->str:
    prefix=artifact.id[:8]
    if artifact.type in _CLIP_TYPES:
        return f"audio/clips/{prefix}_{artifact.filename}"
    if artifact.type in _FULL_AUDIO_TYPES:
        return f"audio/full/{prefix}_{artifact.filename}"
    if artifact.type in _IMAGE_TYPES or artifact.content_type=="image/png":
        return f"images/{prefix}_{artifact.filename}"
    if artifact.type in _REPORT_TYPES or "REPORT" in str(artifact.type):
        return f"report/{prefix}_{artifact.filename}"
    return f"analysis/{prefix}_{artifact.filename}"


def _evidence_bundle_path(evidence:Evidence)->str:
    lower=str(evidence.filename).lower(); prefix=evidence.id[:8]
    if lower.endswith((".pcap",".pcapng")):
        return f"pcap/{prefix}_{evidence.filename}"
    if lower.endswith((".wav",".pcm")):
        return f"audio/full/{prefix}_{evidence.filename}"
    return f"debug/{prefix}_{evidence.filename}"


def _share_safe_evidence(evidence:Evidence)->bool:
    # No raw capture/full audio is copied from Evidence into SHARE_SAFE. Abnormal
    # clips are represented by linked AUDIO_CLIP/PERIODIC_AUDIO_CLIP Artifacts.
    return False


def build_evidence_bundle(db: Session, *, report_id: str, profile: str="INTERNAL_FULL", actor: str|None=None, storage) -> Artifact:
    report=db.get(PreliminaryEvidenceReport,report_id)
    if not report: raise ValueError("EVIDENCE_REPORT_NOT_FOUND")
    profile=str(profile).upper()
    if profile not in {"INTERNAL_FULL","SHARE_SAFE"}: raise ValueError("EVIDENCE_BUNDLE_PROFILE_INVALID")
    artifacts=[a for a in report_artifacts(db,report.id) if _artifact_allowed_for_profile(a,profile)]
    stmt=select(Evidence).where(Evidence.case_id==report.case_id)
    if report.scope_type==EvidenceReportScope.CALL.value and report.call_id:
        stmt=stmt.where((Evidence.call_id==report.call_id)|((Evidence.call_id.is_(None))&(Evidence.session_id==report.session_id)))
    elif report.scope_type==EvidenceReportScope.SESSION.value and report.session_id:
        stmt=stmt.where(Evidence.session_id==report.session_id)
    evidences=list(db.scalars(stmt.order_by(Evidence.created_at.asc())))
    included=evidences if profile=="INTERNAL_FULL" else [e for e in evidences if _share_safe_evidence(e)]
    buf=io.BytesIO(); sums=[]; files=[]
    with zipfile.ZipFile(buf,"w",compression=zipfile.ZIP_DEFLATED,compresslevel=6) as zf:
        for artifact in artifacts:
            try:data=storage.get_bytes(artifact.object_key)
            except Exception:continue
            path=_artifact_bundle_path(artifact); zf.writestr(path,data); sha=hashlib.sha256(data).hexdigest(); sums.append((sha,path))
            files.append({"path":path,"sha256":sha,"source":"artifact","id":artifact.id,"type":artifact.type})
        for evidence in included:
            try:data=storage.get_bytes(evidence.object_key)
            except Exception:continue
            path=_evidence_bundle_path(evidence); zf.writestr(path,data); sha=hashlib.sha256(data).hexdigest(); sums.append((sha,path))
            files.append({"path":path,"sha256":sha,"source":"evidence","id":evidence.id,"type":evidence.type})
        manifest=json.dumps({"schema_version":"evidence-bundle-v1","report_id":report.id,"profile":profile,"created_at":utcnow().isoformat(),
                             "scope":{"type":report.scope_type,"id":report.scope_id},"artifact_count":len(artifacts),"evidence_count":len(included),
                             "profile_boundary":"SHARE_SAFE excludes raw capture and full WAV audio; INTERNAL_FULL includes available scoped raw evidence.",
                             "files":files},ensure_ascii=False,indent=2).encode()
        zf.writestr("manifest.json",manifest); sums.append((hashlib.sha256(manifest).hexdigest(),"manifest.json"))
        zf.writestr("SHA256SUMS","\n".join(f"{sha}  {path}" for sha,path in sorted(sums))+"\n")
    data=buf.getvalue(); row=persist_artifact(db,storage,report=report,artifact_type=EvidenceReportArtifactType.EVIDENCE_BUNDLE.value,
        filename=f"evidence-bundle-{profile.lower()}.zip",data=data,content_type="application/zip",metadata={"profile":profile},role="BUNDLE")
    report.bundle_object_key=row.object_key
    audit(db,case_id=report.case_id,actor=actor,event_type="EVIDENCE_BUNDLE_GENERATED",target_type="artifact",target_id=row.id,
          detail={"report_id":report.id,"profile":profile,"size_bytes":len(data),"artifact_count":len(artifacts),"evidence_count":len(included)})
    db.flush(); return row
