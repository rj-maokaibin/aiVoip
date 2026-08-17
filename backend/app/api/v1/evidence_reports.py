from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import ENGINEER_ROLES, READ_ROLES, get_db, require_roles
from app.core.config import settings
from app.db.evidence_report_models import EvidenceFinding, PreliminaryEvidenceReport
from app.integrations.storage import ObjectStorage
from app.schemas.evidence_reports import EvidenceBundleRequest, EvidenceFindingOut, EvidenceReportOut, EvidenceReportRebuildRequest
from app.services.audit import audit
from app.services.evidence_report import generate_evidence_report
from app.services.evidence_report_artifacts import build_evidence_bundle, report_artifacts
from app.services.idempotency import begin_idempotent, complete_idempotent

router=APIRouter(tags=["evidence-reports"])


def _latest(db: Session, scope_type: str, scope_id: str) -> PreliminaryEvidenceReport | None:
    return db.scalar(select(PreliminaryEvidenceReport).where(
        PreliminaryEvidenceReport.scope_type==scope_type,PreliminaryEvidenceReport.scope_id==scope_id,
    ).order_by(PreliminaryEvidenceReport.version.desc()).limit(1))


def _get_latest_or_404(db: Session, scope_type: str, scope_id: str):
    row=_latest(db,scope_type,scope_id)
    if not row: raise HTTPException(404,"EVIDENCE_REPORT_NOT_FOUND")
    return row


@router.get("/calls/{call_id}/reports/evidence",response_model=EvidenceReportOut)
def call_report(call_id:str,db:Session=Depends(get_db),_identity=Depends(require_roles(*READ_ROLES))):
    return _get_latest_or_404(db,"CALL",call_id)


@router.get("/sessions/{session_id}/reports/evidence",response_model=EvidenceReportOut)
def session_report(session_id:str,db:Session=Depends(get_db),_identity=Depends(require_roles(*READ_ROLES))):
    return _get_latest_or_404(db,"SESSION",session_id)


@router.get("/cases/{case_id}/reports/evidence",response_model=EvidenceReportOut)
def case_report(case_id:str,db:Session=Depends(get_db),_identity=Depends(require_roles(*READ_ROLES))):
    return _get_latest_or_404(db,"CASE",case_id)


def _rebuild(scope_type:str,scope_id:str,req:EvidenceReportRebuildRequest,db:Session,idempotency_key:str|None,identity):
    handle=begin_idempotent(db,scope=f"POST:/api/v1/{scope_type.lower()}/{scope_id}/reports/evidence/rebuild",key=idempotency_key,
                            payload={"scope_type":scope_type,"scope_id":scope_id,"force":req.force})
    if handle.replay is not None: return handle.replay
    try:
        row,_payload,_replay=generate_evidence_report(db,scope_type=scope_type,scope_id=scope_id,actor=identity.actor_id,force=req.force)
        response=EvidenceReportOut.model_validate(row).model_dump(mode="json")
        complete_idempotent(db,handle,response=response,status_code=200,resource_type="preliminary_evidence_report",resource_id=row.id)
        db.commit(); db.refresh(row); return row
    except ValueError as exc:
        db.rollback(); raise HTTPException(404,str(exc))
    except Exception as exc:
        db.rollback(); raise HTTPException(500,f"EVIDENCE_REPORT_GENERATION_FAILED:{type(exc).__name__}:{exc}")


@router.post("/calls/{call_id}/reports/evidence/rebuild",response_model=EvidenceReportOut)
def rebuild_call(call_id:str,req:EvidenceReportRebuildRequest,db:Session=Depends(get_db),idempotency_key:str|None=Header(default=None,alias="Idempotency-Key"),identity=Depends(require_roles(*ENGINEER_ROLES))):
    return _rebuild("CALL",call_id,req,db,idempotency_key,identity)


@router.post("/sessions/{session_id}/reports/evidence/rebuild",response_model=EvidenceReportOut)
def rebuild_session(session_id:str,req:EvidenceReportRebuildRequest,db:Session=Depends(get_db),idempotency_key:str|None=Header(default=None,alias="Idempotency-Key"),identity=Depends(require_roles(*ENGINEER_ROLES))):
    return _rebuild("SESSION",session_id,req,db,idempotency_key,identity)


@router.post("/cases/{case_id}/reports/evidence/rebuild",response_model=EvidenceReportOut)
def rebuild_case(case_id:str,req:EvidenceReportRebuildRequest,db:Session=Depends(get_db),idempotency_key:str|None=Header(default=None,alias="Idempotency-Key"),identity=Depends(require_roles(*ENGINEER_ROLES))):
    return _rebuild("CASE",case_id,req,db,idempotency_key,identity)


@router.get("/reports/evidence/{report_id}/findings",response_model=list[EvidenceFindingOut])
def findings(report_id:str,db:Session=Depends(get_db),_identity=Depends(require_roles(*READ_ROLES))):
    report=db.get(PreliminaryEvidenceReport,report_id)
    if not report: raise HTTPException(404,"EVIDENCE_REPORT_NOT_FOUND")
    rows=list(db.scalars(select(EvidenceFinding).where(EvidenceFinding.scope_type==report.scope_type,EvidenceFinding.scope_id==report.scope_id)
                         .order_by(EvidenceFinding.representative_time.asc().nullslast())))
    return rows


@router.get("/reports/evidence/{report_id}/artifacts")
def artifacts(report_id:str,db:Session=Depends(get_db),_identity=Depends(require_roles(*READ_ROLES))):
    if not db.get(PreliminaryEvidenceReport,report_id): raise HTTPException(404,"EVIDENCE_REPORT_NOT_FOUND")
    return [{"id":a.id,"type":a.type,"filename":a.filename,"content_type":a.content_type,"size_bytes":a.size_bytes,"sha256":a.sha256,"metadata":a.metadata_json or {}} for a in report_artifacts(db,report_id)]


@router.get("/reports/evidence/{report_id}/links")
def links(report_id:str,db:Session=Depends(get_db),_identity=Depends(require_roles(*READ_ROLES))):
    row=db.get(PreliminaryEvidenceReport,report_id)
    if not row: raise HTTPException(404,"EVIDENCE_REPORT_NOT_FOUND")
    storage=ObjectStorage(); ttl=timedelta(minutes=settings.artifact_url_ttl_minutes)
    def url(key): return storage.presigned_get(key,ttl) if key else None
    return {"html_url":url(row.html_object_key),"json_url":url(row.json_object_key),"manifest_url":url(row.manifest_object_key),
            "bundle_url":url(row.bundle_object_key),"expires_minutes":settings.artifact_url_ttl_minutes}


@router.post("/reports/evidence/{report_id}/bundle")
def create_bundle(report_id:str,req:EvidenceBundleRequest,db:Session=Depends(get_db),identity=Depends(require_roles(*ENGINEER_ROLES))):
    try:
        storage=ObjectStorage(); artifact=build_evidence_bundle(db,report_id=report_id,profile=req.profile,actor=identity.actor_id,storage=storage)
        audit(db,case_id=artifact.case_id,actor=identity.actor_id,event_type="EVIDENCE_BUNDLE_DOWNSTREAM_READY",target_type="artifact",target_id=artifact.id,
              detail={"report_id":report_id,"profile":req.profile})
        db.commit(); return {"artifact_id":artifact.id,"profile":req.profile,"download_url":storage.presigned_get(artifact.object_key),"expires_minutes":settings.artifact_url_ttl_minutes}
    except ValueError as exc:
        db.rollback(); raise HTTPException(404,str(exc))
    except Exception as exc:
        db.rollback(); raise HTTPException(500,f"EVIDENCE_BUNDLE_FAILED:{type(exc).__name__}:{exc}")
