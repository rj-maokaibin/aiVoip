from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.contracts.evidence_report import REPORT_COMPOSER_VERSION, REPORT_SCHEMA_VERSION, EvidenceFindingStatus, EvidenceReportArtifactType, EvidenceReportStatus
from app.db.evidence_report_models import EvidenceFinding, PreliminaryEvidenceReport
from app.integrations.storage import ObjectStorage
from app.reports.evidence_brief import build_report_payload, canonical_hash, render_report_html
from app.services.audit import audit
from app.services.evidence_boundary import apply_first_observable_boundaries
from app.services.evidence_report_aggregation import enrich_aggregate_payload
from app.services.evidence_report_analysis_artifacts import materialize_analyzer_json_artifacts
from app.services.evidence_report_artifacts import build_manifest, generate_visual_artifacts, persist_artifact
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
    case=scope["case"]; session=scope.get("session"); call=scope.get("call")
    evidences=scoped_evidences(db,scope_type=scope_type,scope=scope); evidence_items=[evidence_dict(e) for e in evidences]; evidence_ids={e.id for e in evidences}
    runs=latest_analyzer_runs(db,case_id=case.id,evidence_ids=evidence_ids,case_scope=scope_type=="CASE")
    results,states=load_analyzer_results(storage,runs); previous=latest_report(db,scope_type,scope_id); version=(previous.version+1) if previous else 1
    environment=environment_snapshot(db,case,session)
    payload=build_report_payload(case=case_dict(case),scope_type=scope_type,scope_id=scope_id,session=session_dict(session),call=call_dict(call),
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
    apply_first_observable_boundaries(payload); enrich_aggregate_payload(db,payload=payload,scope_type=scope_type,case_id=case.id,session_id=session.id if session else None)
    payload["input_snapshot_hash"]=canonical_hash({"base":payload["input_snapshot_hash"],"findings":payload.get("findings"),"multi_call_summary":payload.get("multi_call_summary"),
                                                     "environment_groups":payload.get("environment_groups"),"ab_comparison":payload.get("ab_comparison"),
                                                     "evidence_retention":payload.get("evidence_retention")})
    idem=report_idempotency_key(scope_type,scope_id,payload["input_snapshot_hash"],states,forced_version=version if force else None)
    if not force:
        same=db.scalar(select(PreliminaryEvidenceReport).where(PreliminaryEvidenceReport.idempotency_key==idem).limit(1))
        if same:return same,same.snapshot_json or payload,True
    report=PreliminaryEvidenceReport(case_id=case.id,session_id=session.id if session else None,call_id=call.id if call else None,scope_type=scope_type,scope_id=scope_id,
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
    visuals=generate_visual_artifacts(db,storage,report=report,results=results,runs=runs)
    report_artifacts=source_artifacts+analysis_artifacts+visuals
    payload["artifacts"]=[{"artifact_id":a.id,"type":a.type,"filename":a.filename,"content_type":a.content_type,"sha256":a.sha256,"metadata":a.metadata_json or {}} for a in report_artifacts]
    for item in payload.get("findings",[]):
        refs=finding_artifact_refs(db,report_id=report.id,finding_id=item.get("finding_id")); item["artifact_refs"]=refs
        row=next((r for r in finding_rows if r.id==item.get("finding_id")),None)
        if row:row.artifact_refs_json=refs
    html_bytes=render_report_html(payload).encode("utf-8"); json_bytes=json.dumps(payload,ensure_ascii=False,indent=2).encode("utf-8")
    json_art=persist_artifact(db,storage,report=report,artifact_type=EvidenceReportArtifactType.PRELIMINARY_REPORT_JSON.value,filename="preliminary-evidence-report.json",data=json_bytes,content_type="application/json",metadata={"schema_version":REPORT_SCHEMA_VERSION},role="REPORT")
    html_art=persist_artifact(db,storage,report=report,artifact_type=EvidenceReportArtifactType.PRELIMINARY_REPORT_HTML.value,filename="preliminary-evidence-report.html",data=html_bytes,content_type="text/html; charset=utf-8",metadata={"schema_version":REPORT_SCHEMA_VERSION},role="REPORT")
    report.json_object_key=json_art.object_key; report.html_object_key=html_art.object_key
    manifest=build_manifest(report,report_artifacts+[json_art,html_art]); manifest_bytes=json.dumps(manifest,ensure_ascii=False,indent=2).encode("utf-8")
    manifest_art=persist_artifact(db,storage,report=report,artifact_type=EvidenceReportArtifactType.MANIFEST_JSON.value,filename="manifest.json",data=manifest_bytes,content_type="application/json",metadata={"manifest_schema":"evidence-bundle-manifest-v1"},role="MANIFEST")
    report.manifest_object_key=manifest_art.object_key
    report.status=EvidenceReportStatus.COMPLETE.value if payload.get("completeness",{}).get("state")=="COMPLETE" else EvidenceReportStatus.PARTIAL_COMPLETE.value
    report.snapshot_json=payload; report.completed_at=utcnow(); db.flush()
    audit(db,case_id=case.id,actor=actor,event_type="PRELIMINARY_EVIDENCE_REPORT_GENERATED",target_type="preliminary_evidence_report",target_id=report.id,
          detail={"scope_type":scope_type,"scope_id":scope_id,"version":version,"status":report.status,"finding_count":len(finding_rows),"artifact_count":len(report_artifacts)+3,"forced":force,"expired_raw_evidence_count":len(expired)})
    return report,payload,False


def mark_report_failed(db: Session, report: PreliminaryEvidenceReport, exc: Exception) -> None:
    report.status=EvidenceReportStatus.FAILED.value; report.error_code=type(exc).__name__; report.error_message=str(exc); report.completed_at=utcnow()
    audit(db,case_id=report.case_id,event_type="PRELIMINARY_EVIDENCE_REPORT_FAILED",target_type="preliminary_evidence_report",target_id=report.id,
          detail={"scope_type":report.scope_type,"scope_id":report.scope_id,"error_code":type(exc).__name__}); db.flush()
