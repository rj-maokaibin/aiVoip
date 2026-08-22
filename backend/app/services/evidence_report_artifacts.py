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
    render_rtp_timeline_png, render_spectrum_png,
    render_spectrogram_png, render_waveform_png, visual_metadata,
)
from app.reports.sip_flow_visual import render_sip_call_flow_png
from app.services.audit import audit


_PCM_FOCUSED_VISUAL_TYPES={
    "PCM_GAP","UNEXPECTED_SILENCE","CLICK_POP","PERIODIC_LOW_FREQUENCY_INTERFERENCE",
    "LOCAL_CAPTURE_PERIODIC_INTERFERENCE","PERIODIC_INTERFERENCE_PATH_COMPARISON","ECHO_PATH_DETECTED","DTMF_ABNORMAL",
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


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
    db.add(row);db.flush()
    db.add(EvidenceReportArtifactLink(report_id=report.id,artifact_id=row.id,finding_ids_json=finding_ids or [],role=role));db.flush()
    return row


def _media_json_artifacts(db: Session, storage, media_run: AnalyzerRun | None) -> list[tuple[Artifact,dict]]:
    if not media_run:return []
    rows=list(db.scalars(select(Artifact).where(
        Artifact.analyzer_run_id==media_run.id,
        Artifact.type.in_(["WAVEFORM_JSON","SPECTROGRAM_JSON"]),
    ).order_by(Artifact.created_at.asc())))
    out=[]
    for row in rows:
        try:out.append((row,json.loads(storage.get_bytes(row.object_key).decode("utf-8"))))
        except Exception:continue
    return out


def _current_findings(db:Session,report:PreliminaryEvidenceReport)->list[EvidenceFinding]:
    return list(db.scalars(select(EvidenceFinding).where(
        EvidenceFinding.scope_type==report.scope_type,
        EvidenceFinding.scope_id==report.scope_id,
        EvidenceFinding.last_seen_report_version==report.version,
    ).order_by(EvidenceFinding.representative_time.asc())))


def _direction(stream:dict)->str:
    return f"{stream.get('src_ip')}:{stream.get('src_port')}->{stream.get('dst_ip')}:{stream.get('dst_port')}"


def _finding_window(f:EvidenceFinding)->tuple[float|None,float|None,float|None]:
    start=float(f.start_time) if f.start_time is not None else None
    end=float(f.end_time) if f.end_time is not None else start
    rep=float(f.representative_time) if f.representative_time is not None else start
    return start,end,rep


def _pcm_session_lookup(pcm:dict)->dict[tuple[str,int],dict]:
    out={}
    for stream in pcm.get("streams",[]) or []:
        tap=str((stream.get("tap") or {}).get("name") or "")
        direction=(stream.get("tap") or {}).get("direction")
        for session in stream.get("sessions",[]) or []:
            out[(tap,int(session.get("session_index") or 0))]={**session,"direction":direction}
    return out


def _scope_matches_pcm(f:EvidenceFinding,tap:str,session_index:int)->bool:
    scope=f.scope_json or {}
    if scope.get("pcm_tap")!=tap:return False
    idx=scope.get("pcm_session_index")
    return idx is None or int(idx)==int(session_index)


def _overlaps(start:float|None,end:float|None,lo:float|None,hi:float|None)->bool:
    if None in {start,end,lo,hi}:return True
    return float(end)>=float(lo) and float(start)<=float(hi)


def _anomaly_relative(f:EvidenceFinding,session:dict,duration:float)->tuple[float|None,float|None,dict]:
    start,end,rep=_finding_window(f);base=session.get("start_time")
    if base is None:return None,None,{}
    base=float(base);use_start=rep if start is None else start;use_end=end if end is not None else use_start
    if use_start is None:return None,None,{}
    a=max(0.0,min(duration,float(use_start)-base));b=max(a,min(duration,float(use_end)-base if use_end is not None else a))
    if b-a < 0.002:b=min(duration,a+0.02)
    return a,b,{"absolute_start":use_start,"absolute_end":use_end,"relative_start_seconds":round(a,6),"relative_end_seconds":round(b,6)}


def _focused_rtp_stream(stream:dict,f:EvidenceFinding)->dict:
    _,_,rep=_finding_window(f)
    if rep is None:return stream
    window_start=max(float(stream.get("start_time") or rep),rep-1.0)
    window_end=min(float(stream.get("end_time") or rep),rep+1.0)
    events=[e for e in stream.get("events",[]) or [] if window_start<=float(e.get("start_time") or rep)<=window_end]
    return {**stream,"start_time":window_start,"end_time":window_end,"events":events}


def generate_visual_artifacts(db: Session, storage, *, report: PreliminaryEvidenceReport,
                              results: dict[str,dict|None], runs: dict[str,AnalyzerRun]) -> list[Artifact]:
    """Generate deterministic summary visuals and exact Finding-scoped views."""
    created=[];packet=results.get("packet_intelligence") or {};pcm=results.get("pcm_intelligence") or {}
    packet_run=runs.get("packet_intelligence");pcm_run=runs.get("pcm_intelligence");media_run=runs.get("media_intelligence")
    findings=_current_findings(db,report);streams={str(x.get("stream_id")):x for x in packet.get("rtp_streams",[]) or []}

    if streams:
        created.append(persist_artifact(db,storage,report=report,artifact_type=EvidenceReportArtifactType.RTP_TIMELINE_PNG.value,
            filename="rtp_timeline.png",data=render_rtp_timeline_png(list(streams.values()),title="RTP TIMELINE - ALL STREAMS",subtitle=f"CALL {report.call_id or report.scope_id}"),content_type="image/png",
            metadata=visual_metadata("RTP_TIMELINE",source={"analyzer_run_id":packet_run.id if packet_run else None},title="RTP Timeline - All Streams",
                x_axis="Time from first RTP",y_axis="RTP Stream / Event",units={"x":"s"},call_id=report.call_id,caption="全局 RTP Stream 与异常事件概览。"),
            analyzer_run_id=packet_run.id if packet_run else None,role="SUMMARY"))
    if packet.get("calls"):
        sip_findings=[f.id for f in findings if str(f.finding_type).startswith("SIP_") or f.finding_type=="CODEC_NEGOTIATION_MISMATCH"]
        created.append(persist_artifact(db,storage,report=report,artifact_type=EvidenceReportArtifactType.SIP_CALL_FLOW_PNG.value,
            filename="sip_call_flow.png",data=render_sip_call_flow_png(packet.get("calls") or [],title="SIP CALL FLOW",subtitle=f"CALL {report.call_id or report.scope_id}"),content_type="image/png",
            metadata=visual_metadata("SIP_CALL_FLOW",source={"analyzer_run_id":packet_run.id if packet_run else None},title="SIP Call Flow",
                x_axis="SIP endpoint",y_axis="Message order",units={"frame":"Frame"},finding_ids=sip_findings,call_id=report.call_id,
                caption="SIP Call ladder：每条消息优先显示 Frame 与 Method/Status。"),
            analyzer_run_id=packet_run.id if packet_run else None,finding_ids=sip_findings,role="FINDING" if sip_findings else "SUMMARY"))

    rtp_visual_count=0
    for finding in findings:
        scope=finding.scope_json or {};stream_id=scope.get("rtp_stream_id");stream=streams.get(str(stream_id)) if stream_id else None
        if not stream or rtp_visual_count>=12:continue
        if finding.finding_type not in {"HIGH_DELTA","PACKET_LOSS","BURST_LOSS","ONE_WAY_RTP_MEDIA","PAYLOAD_CHANGE"}:continue
        focused=_focused_rtp_stream(stream,finding);start,end,rep=_finding_window(finding);direction=_direction(stream)
        created.append(persist_artifact(db,storage,report=report,artifact_type=EvidenceReportArtifactType.RTP_TIMELINE_PNG.value,
            filename=f"finding_{finding.id[:8]}_rtp_timeline.png",
            data=render_rtp_timeline_png([focused],title=f"RTP {finding.finding_type}",subtitle=direction),content_type="image/png",
            metadata=visual_metadata("RTP_TIMELINE",source={"analyzer_run_id":packet_run.id if packet_run else None,"stream_id":stream_id},
                window={"start":focused.get("start_time"),"end":focused.get("end_time")},title=f"RTP {finding.finding_type}",x_axis="Time around anomaly",y_axis="RTP Stream / Event",
                units={"x":"s","sequence":"RTP Seq","frame":"Frame"},legend=[finding.finding_type],finding_ids=[finding.id],call_id=scope.get("call_id") or report.call_id,direction=direction,
                anomaly_window={"start":start,"end":end,"representative":rep},caption=f"{finding.title}：异常点附近 +/-1s RTP Timeline。"),
            analyzer_run_id=packet_run.id if packet_run else None,finding_ids=[finding.id],role="FINDING"));rtp_visual_count+=1

    spectra=0
    for stream in pcm.get("streams",[]) or []:
        tap=(stream.get("tap") or {}).get("name") or "pcm";direction=(stream.get("tap") or {}).get("direction")
        for sess in stream.get("sessions",[]) or []:
            hum=sess.get("hum") or {};spectral=sess.get("spectral") or {}
            if str(hum.get("level") or "LOW").upper() not in {"MEDIUM","HIGH"} or not spectral or spectra>=4:continue
            related=[f.id for f in findings if f.finding_type in {"PERIODIC_LOW_FREQUENCY_INTERFERENCE","LOCAL_CAPTURE_PERIODIC_INTERFERENCE","PERIODIC_INTERFERENCE_PATH_COMPARISON"} and _scope_matches_pcm(f,str(tap),int(sess.get("session_index") or 0))]
            if not related:continue
            title=f"SPECTRUM {str(tap).upper()} PERIODIC INTERFERENCE";subtitle=f"SESSION {sess.get('session_index',0)} | {direction or 'UNKNOWN'}"
            created.append(persist_artifact(db,storage,report=report,artifact_type=EvidenceReportArtifactType.SPECTRUM_PNG.value,
                filename=f"spectrum_{tap}_{sess.get('session_index',0)}.png",data=render_spectrum_png(spectral,title=title,subtitle=subtitle),content_type="image/png",
                metadata=visual_metadata("SPECTRUM",source={"pcm_tap":tap,"session_index":sess.get("session_index"),"direction":direction},
                    window={"start":sess.get("start_time"),"end":sess.get("end_time")},title=title,x_axis="Frequency",y_axis="Magnitude / Energy ratio",
                    units={"x":"Hz","y":"dB or ratio"},legend=["50/60Hz family reference","spectral peaks"],finding_ids=related,call_id=report.call_id,direction=direction,
                    caption=f"{tap} 周期干扰频谱；参考线用于识别 50/60Hz 及其谐波族，不等于物理根因。"),
                analyzer_run_id=pcm_run.id if pcm_run else None,finding_ids=related,role="FINDING"));spectra+=1

    pcm_sessions=_pcm_session_lookup(pcm);visual_count=0
    for source,data_json in _media_json_artifacts(db,storage,media_run)[:16]:
        meta=source.metadata_json or {};tap=str(meta.get("pcm_tap") or "");session_index=int(meta.get("session_index") or 0)
        session=pcm_sessions.get((tap,session_index))
        if not session:continue
        duration=float(data_json.get("duration_seconds") or (data_json.get("times") or [0])[-1] or 0.0)
        if duration<=0 and session.get("start_time") is not None and session.get("end_time") is not None:duration=max(0.001,float(session["end_time"])-float(session["start_time"]))
        for finding in findings:
            if visual_count>=16:break
            if finding.finding_type not in _PCM_FOCUSED_VISUAL_TYPES:continue
            if not _scope_matches_pcm(finding,tap,session_index):continue
            fstart,fend,_=_finding_window(finding)
            if not _overlaps(fstart,fend,session.get("start_time"),session.get("end_time")):continue
            a,b,anomaly=_anomaly_relative(finding,session,duration);direction=session.get("direction")
            title=f"{source.type.replace('_JSON','')} {str(finding.finding_type)}";subtitle=f"{tap.upper()} SESSION {session_index} | {direction or 'UNKNOWN'}"
            if source.type=="WAVEFORM_JSON":
                data=render_waveform_png(data_json,anomaly_start=a,anomaly_end=b,title=title,subtitle=subtitle)
                atype=EvidenceReportArtifactType.WAVEFORM_PNG.value;suffix="waveform";x_axis="Time";y_axis="Amplitude";units={"x":"s","y":"PCM"}
            else:
                data=render_spectrogram_png(data_json,anomaly_start=a,anomaly_end=b,title=title,subtitle=subtitle)
                atype=EvidenceReportArtifactType.SPECTROGRAM_PNG.value;suffix="spectrogram";x_axis="Time";y_axis="Frequency";units={"x":"s","y":"Hz","magnitude":"relative dB"}
            created.append(persist_artifact(db,storage,report=report,artifact_type=atype,
                filename=f"finding_{finding.id[:8]}_{tap}_{session_index}_{suffix}.png",data=data,content_type="image/png",
                metadata=visual_metadata(suffix.upper(),source={"source_artifact_id":source.id,"pcm_tap":tap,"session_index":session_index,"direction":direction},
                    window={"start":session.get("start_time"),"end":session.get("end_time")},title=title,x_axis=x_axis,y_axis=y_axis,units=units,
                    legend=["ANOMALY window"],finding_ids=[finding.id],call_id=(finding.scope_json or {}).get("call_id") or report.call_id,direction=direction,
                    anomaly_window=anomaly,caption=f"{finding.title} 对应 {tap} {suffix}；ANOMALY 标记为 Finding 时间窗。"),
                analyzer_run_id=source.analyzer_run_id,evidence_id=source.evidence_id,finding_ids=[finding.id],role="FINDING"));visual_count+=1
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


def _artifact_type(artifact: Artifact) -> str:
    return str(artifact.type or "").upper()


def _artifact_allowed_for_profile(artifact:Artifact,profile:str)->bool:
    atype=_artifact_type(artifact)
    if atype==EvidenceReportArtifactType.EVIDENCE_BUNDLE.value or str(artifact.filename or "").lower().endswith(".zip"):return False
    if profile=="INTERNAL_FULL":return True
    return atype not in _FULL_AUDIO_TYPES


def _artifact_bundle_path(artifact:Artifact)->str:
    prefix=artifact.id[:8];atype=_artifact_type(artifact)
    if atype in _CLIP_TYPES:return f"audio/clips/{prefix}_{artifact.filename}"
    if atype in _FULL_AUDIO_TYPES:return f"audio/full/{prefix}_{artifact.filename}"
    if atype in _IMAGE_TYPES or artifact.content_type=="image/png":return f"images/{prefix}_{artifact.filename}"
    if atype in _REPORT_TYPES or "REPORT" in atype:return f"report/{prefix}_{artifact.filename}"
    return f"analysis/{prefix}_{artifact.filename}"


def _evidence_bundle_path(evidence:Evidence)->str:
    lower=str(evidence.filename).lower();prefix=evidence.id[:8]
    if lower.endswith((".pcap",".pcapng")):return f"pcap/{prefix}_{evidence.filename}"
    if lower.endswith((".wav",".pcm")):return f"audio/full/{prefix}_{evidence.filename}"
    return f"debug/{prefix}_{evidence.filename}"


def _share_safe_evidence(evidence:Evidence)->bool:
    return False


def build_evidence_bundle(db: Session, *, report_id: str, profile: str="INTERNAL_FULL", actor: str|None=None, storage) -> Artifact:
    report=db.get(PreliminaryEvidenceReport,report_id)
    if not report:raise ValueError("EVIDENCE_REPORT_NOT_FOUND")
    profile=str(profile).upper()
    if profile not in {"INTERNAL_FULL","SHARE_SAFE"}:raise ValueError("EVIDENCE_BUNDLE_PROFILE_INVALID")
    artifacts=[a for a in report_artifacts(db,report.id) if _artifact_allowed_for_profile(a,profile)]
    stmt=select(Evidence).where(Evidence.case_id==report.case_id)
    if report.scope_type==EvidenceReportScope.CALL.value and report.call_id:
        stmt=stmt.where((Evidence.call_id==report.call_id)|((Evidence.call_id.is_(None))&(Evidence.session_id==report.session_id)))
    elif report.scope_type==EvidenceReportScope.SESSION.value and report.session_id:
        stmt=stmt.where(Evidence.session_id==report.session_id)
    evidences=list(db.scalars(stmt.order_by(Evidence.created_at.asc())))
    included=evidences if profile=="INTERNAL_FULL" else [e for e in evidences if _share_safe_evidence(e)]
    buf=io.BytesIO();sums=[];files=[]
    with zipfile.ZipFile(buf,"w",compression=zipfile.ZIP_DEFLATED,compresslevel=6) as zf:
        for artifact in artifacts:
            try:data=storage.get_bytes(artifact.object_key)
            except Exception:continue
            path=_artifact_bundle_path(artifact);zf.writestr(path,data);sha=hashlib.sha256(data).hexdigest();sums.append((sha,path))
            files.append({"path":path,"sha256":sha,"source":"artifact","id":artifact.id,"type":artifact.type})
        for evidence in included:
            try:data=storage.get_bytes(evidence.object_key)
            except Exception:continue
            path=_evidence_bundle_path(evidence);zf.writestr(path,data);sha=hashlib.sha256(data).hexdigest();sums.append((sha,path))
            files.append({"path":path,"sha256":sha,"source":"evidence","id":evidence.id,"type":evidence.type})
        manifest=json.dumps({"schema_version":"evidence-bundle-v1","report_id":report.id,"profile":profile,"created_at":utcnow().isoformat(),
                             "scope":{"type":report.scope_type,"id":report.scope_id},"artifact_count":len(artifacts),"evidence_count":len(included),
                             "profile_boundary":"SHARE_SAFE excludes raw capture and full WAV audio; INTERNAL_FULL includes available scoped raw evidence.",
                             "files":files},ensure_ascii=False,indent=2).encode()
        zf.writestr("manifest.json",manifest);sums.append((hashlib.sha256(manifest).hexdigest(),"manifest.json"))
        zf.writestr("SHA256SUMS","\n".join(f"{sha}  {path}" for sha,path in sorted(sums))+"\n")
    data=buf.getvalue();row=persist_artifact(db,storage,report=report,artifact_type=EvidenceReportArtifactType.EVIDENCE_BUNDLE.value,
        filename=f"evidence-bundle-{profile.lower()}.zip",data=data,content_type="application/zip",metadata={"profile":profile},role="BUNDLE")
    report.bundle_object_key=row.object_key
    audit(db,case_id=report.case_id,actor=actor,event_type="EVIDENCE_BUNDLE_GENERATED",target_type="artifact",target_id=row.id,
          detail={"report_id":report.id,"profile":profile,"size_bytes":len(data),"artifact_count":len(artifacts),"evidence_count":len(included)})
    db.flush();return row
