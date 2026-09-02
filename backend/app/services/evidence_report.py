from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.contracts.evidence_report import AnalysisMode, REPORT_COMPOSER_VERSION, REPORT_SCHEMA_VERSION, EvidenceFindingStatus, EvidenceReportArtifactType, EvidenceReportStatus
from app.db.evidence_report_models import EvidenceFinding, PreliminaryEvidenceReport
from app.db.models import ReproductionCall, ReproductionEventRecord, ReproductionSession
from app.integrations.storage import ObjectStorage
from app.reports.evidence_brief import build_report_payload, canonical_hash, render_report_html
from app.reports.prd_spec_v1_alignment import finalize_report_contract
from app.services.audit import audit
from app.services.evidence_boundary import apply_first_observable_boundaries
from app.services.evidence_report_aggregation import enrich_aggregate_payload
from app.services.evidence_report_analysis_artifacts import materialize_analyzer_json_artifacts
from app.services.evidence_report_artifacts import build_manifest, generate_visual_artifacts, persist_artifact
from app.services.evidence_report_context import resolve_report_analysis_context
from app.services.evidence_report_source_artifacts import finding_artifact_refs, link_source_artifacts
from app.services.evidence_report_scope import (
    call_dict, case_dict, environment_snapshot, evidence_dict, latest_analyzer_runs,
    load_analyzer_results, resolve_scope, scope_value, scoped_evidences, session_dict,
)


def utcnow() -> datetime: return datetime.now(timezone.utc)


def latest_report(db: Session, scope_type: str, scope_id: str) -> PreliminaryEvidenceReport | None:
    return db.scalar(select(PreliminaryEvidenceReport).where(
        PreliminaryEvidenceReport.scope_type==scope_type,PreliminaryEvidenceReport.scope_id==scope_id,
    ).order_by(PreliminaryEvidenceReport.version.desc()).limit(1))


def report_idempotency_key(scope_type: str, scope_id: str, input_hash: str, analyzer_states: dict, *, forced_version: int|None=None) -> str:
    versions={k:{"run_id":v.get("run_id"),"analyzer_version":v.get("analyzer_version"),"config_version":v.get("config_version")} for k,v in analyzer_states.items()}
    material={"scope_type":scope_type,"scope_id":scope_id,"input_snapshot_hash":input_hash,"schema_version":REPORT_SCHEMA_VERSION,
              "composer_version":REPORT_COMPOSER_VERSION,"analyzer_versions":versions}
    if forced_version is not None: material["forced_rebuild_version"]=forced_version
    return canonical_hash(material)


def _analysis_context_source_analyzer(results: dict[str,dict|None]) -> str | None:
    """Mirror authoritative packet-source selection without re-analysis."""
    if results.get("packet_intelligence") is not None:
        return "packet_intelligence"
    media_packet=((results.get("media_intelligence") or {}).get("packet"))
    if media_packet is not None:
        return "media_intelligence"
    return None


def _visual_source_results(results: dict[str,dict|None]) -> dict[str,dict|None]:
    """Use the same packet/PCM fallback contract as the canonical report builder.

    Media intelligence can be the only persisted report analyzer while still
    carrying authoritative ``packet`` and ``pcm`` projections. Findings already
    consume those projections through ``build_report_payload``; visual generation
    must therefore see the exact same sources or RG-011 can incorrectly block a
    publishable report because its Finding-scoped primary visual was never built.
    Standalone packet/PCM analyzers, when present, remain authoritative.
    """
    resolved=dict(results)
    media=results.get("media_intelligence") or {}
    if isinstance(media,dict):
        if resolved.get("packet_intelligence") is None:
            resolved["packet_intelligence"]=media.get("packet")
        if resolved.get("pcm_intelligence") is None:
            resolved["pcm_intelligence"]=media.get("pcm")
    return resolved


def _analysis_context_evidences(evidence_items: list[dict], runs: dict, results: dict[str,dict|None]) -> tuple[list[dict], list[str], str | None]:
    """Limit Call-context binding to the AnalyzerRun that supplied packet facts.

    CASE scope can contain old captures from earlier reproductions/imports, and the
    latest Packet/PCM/Media runs can be produced at different times. The Call
    context therefore follows only the AnalyzerRun whose packet result is actually
    used by the context resolver, rather than a union of unrelated Analyzer inputs.
    """
    analyzer_name=_analysis_context_source_analyzer(results)
    run=runs.get(analyzer_name) if analyzer_name else None
    input_ids={str(x) for x in ((run.input_evidence_ids or []) if run else []) if x}
    if not input_ids:
        return evidence_items, [], analyzer_name
    selected=[x for x in evidence_items if str(x.get("id") or "") in input_ids]
    return selected, sorted(input_ids), analyzer_name


def _is_packet_evidence(item: dict) -> bool:
    return str(item.get("type") or "").upper() in {"PCAP","PCAPNG"} or str(item.get("original_type") or "").upper() in {"PCAP","PCAPNG"}


def _case_runtime_scope_from_evidence(
    db: Session,
    *,
    case_id: str,
    scope_type: str,
    context_evidences: list[dict],
    fallback_session,
    fallback_call,
) -> tuple[object | None, object | None, dict]:
    """Resolve CASE runtime Session/Call from the current packet Evidence binding.

    `resolve_scope(CASE)` exposes the latest historical runtime rows for convenience,
    but those rows are not authoritative for a specific AnalyzerRun. If the current
    packet Evidence is explicitly bound, follow those IDs. If it is unbound, keep
    the historical rows only as suppressed metadata for the Offline resolver.
    Ambiguous/missing bound IDs fail closed instead of silently selecting "latest".
    """
    if str(scope_type).upper() != "CASE":
        return fallback_session,fallback_call,{"source":"EXPLICIT_SCOPE","status":"RESOLVED"}
    packet_rows=[x for x in context_evidences if _is_packet_evidence(x)]
    if not packet_rows:
        return fallback_session,fallback_call,{"source":"CASE_FALLBACK_NO_PACKET","status":"FALLBACK"}
    if any(not x.get("session_id") and not x.get("call_id") for x in packet_rows):
        return fallback_session,fallback_call,{"source":"UNBOUND_PACKET_EVIDENCE","status":"SUPPRESSED_BY_OFFLINE_CONTEXT"}

    call_ids={str(x.get("call_id")) for x in packet_rows if x.get("call_id")}
    session_ids={str(x.get("session_id")) for x in packet_rows if x.get("session_id")}
    if len(call_ids)>1:
        return None,None,{"source":"PACKET_EVIDENCE","status":"AMBIGUOUS","call_ids":sorted(call_ids),"session_ids":sorted(session_ids)}
    if len(call_ids)==1:
        call_id=next(iter(call_ids)); bound_call=db.get(ReproductionCall,call_id)
        if bound_call is None or bound_call.case_id!=case_id:
            return None,None,{"source":"PACKET_EVIDENCE_CALL","status":"UNRESOLVED","call_ids":[call_id],"session_ids":sorted(session_ids)}
        bound_session=db.get(ReproductionSession,bound_call.session_id)
        if bound_session is None or bound_session.case_id!=case_id:
            return None,None,{"source":"PACKET_EVIDENCE_CALL","status":"SESSION_UNRESOLVED","call_ids":[call_id],"session_ids":sorted(session_ids)}
        if session_ids and session_ids!={str(bound_session.id)}:
            return None,None,{"source":"PACKET_EVIDENCE_CALL","status":"BINDING_MISMATCH","call_ids":[call_id],"session_ids":sorted(session_ids),"call_session_id":bound_session.id}
        return bound_session,bound_call,{"source":"PACKET_EVIDENCE_CALL","status":"RESOLVED","call_ids":[call_id],"session_ids":[bound_session.id]}

    if len(session_ids)>1:
        return None,None,{"source":"PACKET_EVIDENCE_SESSION","status":"AMBIGUOUS","call_ids":[],"session_ids":sorted(session_ids)}
    if len(session_ids)==1:
        session_id=next(iter(session_ids)); bound_session=db.get(ReproductionSession,session_id)
        if bound_session is None or bound_session.case_id!=case_id:
            return None,None,{"source":"PACKET_EVIDENCE_SESSION","status":"UNRESOLVED","call_ids":[],"session_ids":[session_id]}
        return bound_session,None,{"source":"PACKET_EVIDENCE_SESSION","status":"SESSION_ONLY","call_ids":[],"session_ids":[session_id]}

    return fallback_session,fallback_call,{"source":"CASE_FALLBACK_NO_BINDING_IDS","status":"FALLBACK"}


def _runtime_binding_ids(analysis_context: dict, session, call) -> tuple[str | None, str | None]:
    """Return persisted runtime FK bindings only for genuine reproduction context."""
    if analysis_context.get("analysis_mode") != AnalysisMode.REPRODUCTION.value:
        return None, None
    return (session.id if session else None, call.id if call else None)


def _session_runtime_call_count(db: Session, session: ReproductionSession | None) -> int | None:
    if session is None:
        return None
    return len(list(db.scalars(select(ReproductionCall.id).where(ReproductionCall.session_id==session.id))))


def _session_events(db: Session, session: ReproductionSession | None) -> list[dict]:
    """Persist report-facing reproduction events without re-parsing Debug text.

    FR-028 requires pre-Call facts such as OFFHOOK to survive even when no valid
    Call is formed. ReproductionEventRecord is the authoritative structured source;
    the report must not infer OFFHOOK merely from the presence of a Debug artifact.
    """
    if session is None:
        return []
    rows=list(db.scalars(select(ReproductionEventRecord).where(
        ReproductionEventRecord.session_id==session.id,
    ).order_by(ReproductionEventRecord.session_relative_ms.asc(),ReproductionEventRecord.created_at.asc())))
    return [{
        "event_id":row.id,
        "event_type":row.event_type,
        "source":row.source,
        "anchor_type":row.anchor_type,
        "session_relative_ms":row.session_relative_ms,
        "source_timestamp":row.source_timestamp,
        "timestamp_source":row.timestamp_source,
        "uncertainty_ms":row.uncertainty_ms,
        "payload":row.payload_json or {},
        "created_at":row.created_at.isoformat() if row.created_at else None,
    } for row in rows]


def _persist_findings(db: Session, *, report: PreliminaryEvidenceReport, payload: dict) -> list[EvidenceFinding]:
    existing={x.stable_key:x for x in db.scalars(select(EvidenceFinding).where(EvidenceFinding.scope_type==report.scope_type,EvidenceFinding.scope_id==report.scope_id))}
    observed=set(); rows=[]
    for item in payload.get("findings",[]):
        key=item["stable_key"]; observed.add(key); row=existing.get(key); tr=item.get("time_range") or {}
        attrs={"case_id":report.case_id,"session_id":report.session_id,"call_id":report.call_id,"scope_type":report.scope_type,"scope_id":report.scope_id,
               "stable_key":key,"finding_signature":item["finding_signature"],"signature_version":item["signature_version"],"finding_type":item["type"],
               "severity":item["severity"],"evidence_level":item["evidence_level"],"title":item["title"],"observation":item["observation"],
               "interpretation":item.get("interpretation"),"root_cause_boundary":item["root_cause_boundary"],"start_time":tr.get("start"),"end_time":tr.get("end"),
               "representative_time":tr.get("representative"),"scope_json":item.get("scope") or {},"metrics_json":item.get("metrics") or {},
               "evidence_refs_json":item.get("evidence_refs") or [],"artifact_refs_json":item.get("artifact_refs") or [],"event_refs_json":item.get("event_refs") or [],
               "correlation_json":item.get("correlation") or {},"source_analyzer_run_ids":item.get("source_analyzer_run_ids") or [],
               "occurrence_count":item.get("occurrence_count",1),"last_seen_report_version":report.version}
        if row is None:
            row=EvidenceFinding(status=EvidenceFindingStatus.OBSERVED.value,first_seen_report_version=report.version,**attrs); db.add(row); db.flush()
        else:
            for name,value in attrs.items(): setattr(row,name,value)
            row.status=EvidenceFindingStatus.PERSISTING.value if row.first_seen_report_version<report.version else EvidenceFindingStatus.OBSERVED.value; db.flush()
        item["finding_id"]=row.id; rows.append(row)
    for key,row in existing.items():
        if key not in observed and row.status not in {EvidenceFindingStatus.RESOLVED.value,EvidenceFindingStatus.INVALIDATED.value}:
            row.status=EvidenceFindingStatus.RESOLVED.value; row.last_seen_report_version=report.version
    db.flush(); return rows


def generate_evidence_report(db: Session, *, scope_type, scope_id: str, actor: str|None=None, storage=None, force: bool=False) -> tuple[PreliminaryEvidenceReport,dict,bool]:
    storage=storage or ObjectStorage(); scope_type=scope_value(scope_type); scope=resolve_scope(db,scope_type=scope_type,scope_id=scope_id)
    case=scope["case"]; scope_session=scope.get("session"); scope_call=scope.get("call")
    evidences=scoped_evidences(db,scope_type=scope_type,scope=scope); evidence_items=[evidence_dict(e) for e in evidences]; evidence_ids={e.id for e in evidences}
    runs=latest_analyzer_runs(db,case_id=case.id,evidence_ids=evidence_ids,case_scope=scope_type=="CASE")
    results,states=load_analyzer_results(storage,runs); previous=latest_report(db,scope_type,scope_id); version=(previous.version+1) if previous else 1
    context_evidences,context_input_ids,context_analyzer=_analysis_context_evidences(evidence_items,runs,results)
    session,call,runtime_resolution=_case_runtime_scope_from_evidence(db,case_id=case.id,scope_type=scope_type,context_evidences=context_evidences,
                                                                     fallback_session=scope_session,fallback_call=scope_call)
    runtime_session=session_dict(session); runtime_call=call_dict(call)
    resolved_context=resolve_report_analysis_context(
        scope_type=scope_type,
        session=runtime_session,
        runtime_call=runtime_call,
        evidences=context_evidences,
        results=results,
    )
    analysis_context=resolved_context["analysis_context"]
    analysis_context["analyzer_input_evidence_ids"]=context_input_ids
    analysis_context["context_analyzer"]=context_analyzer
    analysis_context["context_analyzer_run_id"]=(runs.get(context_analyzer).id if context_analyzer and runs.get(context_analyzer) else None)
    analysis_context["runtime_binding_resolution"]=runtime_resolution
    if runtime_resolution.get("status") in {"AMBIGUOUS","UNRESOLVED","SESSION_UNRESOLVED","BINDING_MISMATCH"}:
        issues=list(analysis_context.get("semantic_issues") or [])
        for code in ("REPORT_SEMANTIC_CONTRADICTION","CALL_BINDING_INCOMPLETE"):
            if code not in issues: issues.append(code)
        analysis_context["semantic_issues"]=issues
        analysis_context["semantic_status"]="INCOMPLETE"
        analysis_context["reviewability"]="NOT_FULLY_REVIEWABLE"
    display_call=resolved_context["display_call"]
    runtime_bound=analysis_context.get("analysis_mode")==AnalysisMode.REPRODUCTION.value
    analysis_context["session_runtime_call_count"]=_session_runtime_call_count(db,session) if runtime_bound else None
    analysis_context["session_events"]=_session_events(db,session) if runtime_bound else []
    report_session_id,report_call_id=_runtime_binding_ids(analysis_context,session,call)
    payload_session=runtime_session if runtime_bound else None
    payload_runtime_call=runtime_call if runtime_bound else None
    environment=environment_snapshot(db,case,session if runtime_bound else None)
    payload=build_report_payload(case=case_dict(case),scope_type=scope_type,scope_id=scope_id,
                                 session=payload_session,call=payload_runtime_call,analysis_context=analysis_context,display_call=display_call,
                                 environment=environment,evidences=evidence_items,analyzer_states=states,results=results,report_version=version)
    expired=[{
        "evidence_id":x.get("id"),"original_type":x.get("original_type"),"filename":x.get("filename"),"sha256":x.get("sha256"),
        "retention_status":x.get("retention_status"),"expired_at":x.get("retention_expired_at"),"payload_available":False,
    } for x in evidence_items if not x.get("payload_available",True)]
    payload["evidence_retention"]={"expired_raw_evidence":expired,"expired_count":len(expired)}
    if expired:
        completeness=payload.setdefault("completeness",{})
        completeness["expired_evidence"]=expired
        prior=str(completeness.get("boundary") or "")
        completeness["boundary"]=(f"{len(expired)} 个原始 Evidence Payload 已按 Retention 策略过期；报告、SHA256、来源元数据和关键派生证据仍保留，但过期原始数据不可重新下载或重新分析。 "+prior).strip()
    apply_first_observable_boundaries(payload); enrich_aggregate_payload(db,payload=payload,scope_type=scope_type,case_id=case.id,session_id=report_session_id)
    payload["input_snapshot_hash"]=canonical_hash({"base":payload["input_snapshot_hash"],"findings":payload.get("findings"),"multi_call_summary":payload.get("multi_call_summary"),
                                                     "environment_groups":payload.get("environment_groups"),"ab_comparison":payload.get("ab_comparison"),
                                                     "analysis_context":payload.get("analysis_context"),"display_call":payload.get("display_call"),
                                                     "evidence_retention":payload.get("evidence_retention")})
    idem=report_idempotency_key(scope_type,scope_id,payload["input_snapshot_hash"],states,forced_version=version if force else None)
    if not force:
        same=db.scalar(select(PreliminaryEvidenceReport).where(PreliminaryEvidenceReport.idempotency_key==idem).limit(1))
        if same:return same,same.snapshot_json or payload,True
    report=PreliminaryEvidenceReport(case_id=case.id,session_id=report_session_id,call_id=report_call_id,scope_type=scope_type,scope_id=scope_id,
        version=version,status=EvidenceReportStatus.COMPOSING.value,schema_version=REPORT_SCHEMA_VERSION,composer_version=REPORT_COMPOSER_VERSION,
        input_snapshot_hash=payload["input_snapshot_hash"],idempotency_key=idem,
        analyzer_versions_json={k:{"run_id":v.get("run_id"),"version":v.get("analyzer_version"),"config_version":v.get("config_version")} for k,v in states.items()},
        environment_fingerprint=payload.get("environment_fingerprint"),environment_json=environment,completeness_json=payload.get("completeness"),
        boundary_json=payload.get("evidence_boundary"),supersedes_report_id=previous.id if previous else None,created_by=actor)
    db.add(report); db.flush()
    if previous and previous.status!=EvidenceReportStatus.SUPERSEDED.value:previous.status=EvidenceReportStatus.SUPERSEDED.value
    finding_rows=_persist_findings(db,report=report,payload=payload)
    source_artifacts=link_source_artifacts(db,report=report,runs=runs)
    analysis_artifacts=materialize_analyzer_json_artifacts(db,storage,report=report,runs=runs)
    visuals=generate_visual_artifacts(db,storage,report=report,results=_visual_source_results(results),runs=runs)
    report_artifacts=source_artifacts+analysis_artifacts+visuals
    payload["artifacts"]=[{
        "artifact_id":a.id,"type":a.type,"filename":a.filename,"content_type":a.content_type,"sha256":a.sha256,
        "size_bytes":a.size_bytes,"object_key":a.object_key,"created_at":a.created_at.isoformat() if a.created_at else None,
        "metadata":a.metadata_json or {},
    } for a in report_artifacts]
    for item in payload.get("findings",[]):
        refs=finding_artifact_refs(db,report_id=report.id,finding_id=item.get("finding_id")); item["artifact_refs"]=refs
        row=next((r for r in finding_rows if r.id==item.get("finding_id")),None)
        if row:row.artifact_refs_json=refs
    report.status=EvidenceReportStatus.COMPLETE.value if payload.get("completeness",{}).get("state")=="COMPLETE" else EvidenceReportStatus.PARTIAL_COMPLETE.value
    finalize_report_contract(report,payload)
    html_bytes=render_report_html(payload).encode("utf-8"); json_bytes=json.dumps(payload,ensure_ascii=False,indent=2).encode("utf-8")
    json_art=persist_artifact(db,storage,report=report,artifact_type=EvidenceReportArtifactType.PRELIMINARY_REPORT_JSON.value,filename="preliminary-evidence-report.json",data=json_bytes,content_type="application/json",metadata={"schema_version":REPORT_SCHEMA_VERSION},role="REPORT")
    html_art=persist_artifact(db,storage,report=report,artifact_type=EvidenceReportArtifactType.PRELIMINARY_REPORT_HTML.value,filename="preliminary-evidence-report.html",data=html_bytes,content_type="text/html; charset=utf-8",metadata={"schema_version":REPORT_SCHEMA_VERSION},role="REPORT")
    report.json_object_key=json_art.object_key; report.html_object_key=html_art.object_key
    manifest=build_manifest(report,report_artifacts+[json_art,html_art]); manifest_bytes=json.dumps(manifest,ensure_ascii=False,indent=2).encode("utf-8")
    manifest_art=persist_artifact(db,storage,report=report,artifact_type=EvidenceReportArtifactType.MANIFEST_JSON.value,filename="manifest.json",data=manifest_bytes,content_type="application/json",metadata={"manifest_schema":"evidence-bundle-manifest-v1"},role="MANIFEST")
    report.manifest_object_key=manifest_art.object_key
    report.snapshot_json=payload; report.completed_at=utcnow(); db.flush()
    audit(db,case_id=case.id,actor=actor,event_type="PRELIMINARY_EVIDENCE_REPORT_GENERATED",target_type="preliminary_evidence_report",target_id=report.id,
          detail={"scope_type":scope_type,"scope_id":scope_id,"version":version,"status":report.status,"finding_count":len(finding_rows),"artifact_count":len(report_artifacts)+3,
                  "forced":force,"expired_raw_evidence_count":len(expired),"analysis_mode":analysis_context.get("analysis_mode"),
                  "call_origin":analysis_context.get("call_origin"),"call_scope":analysis_context.get("call_scope"),
                  "reconstructed_call_count":analysis_context.get("reconstructed_call_count",0),"semantic_issues":analysis_context.get("semantic_issues",[]),
                  "context_analyzer":context_analyzer,"analyzer_input_evidence_ids":context_input_ids,"runtime_binding_resolution":runtime_resolution,
                  "persisted_session_id":report_session_id,"persisted_call_id":report_call_id})
    return report,payload,False


def mark_report_failed(db: Session, report: PreliminaryEvidenceReport, exc: Exception) -> None:
    report.status=EvidenceReportStatus.FAILED.value; report.error_code=type(exc).__name__; report.error_message=str(exc); report.completed_at=utcnow()
    audit(db,case_id=report.case_id,event_type="PRELIMINARY_EVIDENCE_REPORT_FAILED",target_type="preliminary_evidence_report",target_id=report.id,
          detail={"scope_type":report.scope_type,"scope_id":report.scope_id,"error_code":type(exc).__name__}); db.flush()