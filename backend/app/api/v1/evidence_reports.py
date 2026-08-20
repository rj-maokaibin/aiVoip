from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.api.evidence_permissions import EvidencePermission, has_evidence_permission, require_evidence_permission
from app.core.config import settings
from app.db.evidence_report_models import EvidenceFinding, PreliminaryEvidenceReport
from app.integrations.storage import ObjectStorage
from app.reports.report_grounding import ReportGroundingError
from app.schemas.evidence_reports import EvidenceBundleRequest, EvidenceFindingOut, EvidenceReportOut, EvidenceReportRebuildRequest
from app.services.audit import audit
from app.services.evidence_report import generate_evidence_report, mark_report_failed
from app.services.evidence_report_artifacts import build_evidence_bundle, report_artifacts
from app.services.idempotency import begin_idempotent, complete_idempotent, fail_idempotent

router=APIRouter(tags=["evidence-reports"])

REPORT_SAFE_ARTIFACT_TYPES = {
    "AUDIO_CLIP", "PERIODIC_AUDIO_CLIP", "WAVEFORM_PNG", "SPECTRUM_PNG", "SPECTROGRAM_PNG",
    "RTP_TIMELINE_PNG", "SIP_CALL_FLOW_PNG", "PRELIMINARY_REPORT_HTML",
    "PRELIMINARY_REPORT_JSON", "MANIFEST_JSON", "WAVEFORM_JSON", "SPECTROGRAM_JSON",
}


def _enabled() -> None:
    if not settings.preliminary_evidence_report_enabled:
        raise HTTPException(503, "PRELIMINARY_EVIDENCE_REPORT_DISABLED")


def _latest(db: Session, scope_type: str, scope_id: str) -> PreliminaryEvidenceReport | None:
    return db.scalar(select(PreliminaryEvidenceReport).where(
        PreliminaryEvidenceReport.scope_type==scope_type,PreliminaryEvidenceReport.scope_id==scope_id,
    ).order_by(PreliminaryEvidenceReport.version.desc()).limit(1))


def _get_latest_or_404(db: Session, scope_type: str, scope_id: str):
    _enabled()
    row=_latest(db,scope_type,scope_id)
    if not row: raise HTTPException(404,"EVIDENCE_REPORT_NOT_FOUND")
    return row


@router.get("/calls/{call_id}/reports/evidence",response_model=EvidenceReportOut)
def call_report(call_id:str,db:Session=Depends(get_db),_identity=Depends(require_evidence_permission(EvidencePermission.VIEW_REPORT))):
    return _get_latest_or_404(db,"CALL",call_id)


@router.get("/sessions/{session_id}/reports/evidence",response_model=EvidenceReportOut)
def session_report(session_id:str,db:Session=Depends(get_db),_identity=Depends(require_evidence_permission(EvidencePermission.VIEW_REPORT))):
    return _get_latest_or_404(db,"SESSION",session_id)


@router.get("/cases/{case_id}/reports/evidence",response_model=EvidenceReportOut)
def case_report(case_id:str,db:Session=Depends(get_db),_identity=Depends(require_evidence_permission(EvidencePermission.VIEW_REPORT))):
    return _get_latest_or_404(db,"CASE",case_id)


def _rebuild(scope_type:str,scope_id:str,req:EvidenceReportRebuildRequest,db:Session,idempotency_key:str|None,identity):
    _enabled()
    handle=begin_idempotent(db,scope=f"POST:/api/v1/{scope_type.lower()}/{scope_id}/reports/evidence/rebuild",key=idempotency_key,
                            payload={"scope_type":scope_type,"scope_id":scope_id,"force":req.force})
    if handle.replay is not None: return handle.replay
    previous=_latest(db,scope_type,scope_id)
    previous_id=previous.id if previous else None
    previous_status=previous.status if previous else None
    try:
        row,_payload,_replay=generate_evidence_report(db,scope_type=scope_type,scope_id=scope_id,actor=identity.actor_id,force=req.force)
        response=EvidenceReportOut.model_validate(row).model_dump(mode="json")
        complete_idempotent(db,handle,response=response,status_code=200,resource_type="preliminary_evidence_report",resource_id=row.id)
        db.commit(); db.refresh(row); return row
    except ReportGroundingError as exc:
        # Grounding is a deterministic publication rejection, not a transient
        # infrastructure crash. Preserve this failed report attempt and its audit
        # trail instead of rolling the whole transaction back and losing the reason.
        failed=_latest(db,scope_type,scope_id)
        if failed is not None and failed.id!=previous_id:
            mark_report_failed(db,failed,exc)
            completeness=dict(failed.completeness_json or {})
            completeness.update({"state":"PARTIAL","reviewability":"NOT_REVIEWABLE","grounding_status":"FAIL"})
            failed.completeness_json=completeness
            failed.snapshot_json={
                "schema_version":failed.schema_version,
                "composer_version":failed.composer_version,
                "report_version":failed.version,
                "scope":{"type":failed.scope_type,"id":failed.scope_id},
                "status":"FAILED",
                "grounding_validation":exc.validation,
                "reviewability_status":"NOT_REVIEWABLE",
            }
            audit(db,case_id=failed.case_id,actor=identity.actor_id,event_type="PRELIMINARY_EVIDENCE_REPORT_GROUNDING_BLOCKED",
                  target_type="preliminary_evidence_report",target_id=failed.id,
                  detail={"scope_type":scope_type,"scope_id":scope_id,"report_version":failed.version,
                          "grounding_status":exc.validation.get("status"),"counts":exc.validation.get("counts"),
                          "issues":(exc.validation.get("issues") or [])[:20]})
        if previous is not None and previous.id==previous_id and previous_status is not None:
            # generate_evidence_report marks the previous report SUPERSEDED before
            # final publication. A grounding-blocked replacement must not retire
            # the last successfully generated report.
            previous.status=previous_status
        fail_idempotent(db,handle)
        db.commit()
        raise HTTPException(422,f"REPORT_GROUNDING_FAILED:{exc}")
    except ValueError as exc:
        db.rollback(); raise HTTPException(404,str(exc))
    except Exception as exc:
        db.rollback(); raise HTTPException(500,f"EVIDENCE_REPORT_GENERATION_FAILED:{type(exc).__name__}:{exc}")


@router.post("/calls/{call_id}/reports/evidence/rebuild",response_model=EvidenceReportOut)
def rebuild_call(call_id:str,req:EvidenceReportRebuildRequest,db:Session=Depends(get_db),idempotency_key:str|None=Header(default=None,alias="Idempotency-Key"),identity=Depends(require_evidence_permission(EvidencePermission.REBUILD_REPORT))):
    return _rebuild("CALL",call_id,req,db,idempotency_key,identity)


@router.post("/sessions/{session_id}/reports/evidence/rebuild",response_model=EvidenceReportOut)
def rebuild_session(session_id:str,req:EvidenceReportRebuildRequest,db:Session=Depends(get_db),idempotency_key:str|None=Header(default=None,alias="Idempotency-Key"),identity=Depends(require_evidence_permission(EvidencePermission.REBUILD_REPORT))):
    return _rebuild("SESSION",session_id,req,db,idempotency_key,identity)


@router.post("/cases/{case_id}/reports/evidence/rebuild",response_model=EvidenceReportOut)
def rebuild_case(case_id:str,req:EvidenceReportRebuildRequest,db:Session=Depends(get_db),idempotency_key:str|None=Header(default=None,alias="Idempotency-Key"),identity=Depends(require_evidence_permission(EvidencePermission.REBUILD_REPORT))):
    return _rebuild("CASE",case_id,req,db,idempotency_key,identity)


@router.get("/reports/evidence/{report_id}/findings",response_model=list[EvidenceFindingOut])
def findings(report_id:str,db:Session=Depends(get_db),_identity=Depends(require_evidence_permission(EvidencePermission.VIEW_REPORT))):
    _enabled()
    report=db.get(PreliminaryEvidenceReport,report_id)
    if not report: raise HTTPException(404,"EVIDENCE_REPORT_NOT_FOUND")
    rows=list(db.scalars(select(EvidenceFinding).where(EvidenceFinding.scope_type==report.scope_type,EvidenceFinding.scope_id==report.scope_id)
                         .order_by(EvidenceFinding.representative_time.asc().nullslast())))
    return rows


@router.get("/reports/evidence/{report_id}/artifacts")
def artifacts(report_id:str,db:Session=Depends(get_db),identity=Depends(require_evidence_permission(EvidencePermission.VIEW_REPORT))):
    _enabled()
    if not db.get(PreliminaryEvidenceReport,report_id): raise HTTPException(404,"EVIDENCE_REPORT_NOT_FOUND")
    rows=report_artifacts(db,report_id)
    if not has_evidence_permission(identity,EvidencePermission.VIEW_RAW_EVIDENCE):
        rows=[a for a in rows if str(a.type or "").upper() in REPORT_SAFE_ARTIFACT_TYPES]
    return [{"id":a.id,"type":a.type,"filename":a.filename,"content_type":a.content_type,"size_bytes":a.size_bytes,"sha256":a.sha256,"metadata":a.metadata_json or {},"content_url":f"/api/v1/artifacts/{a.id}/content"} for a in rows]


@router.get("/reports/evidence/{report_id}/links")
def links(report_id:str,db:Session=Depends(get_db),identity=Depends(require_evidence_permission(EvidencePermission.VIEW_REPORT))):
    _enabled()
    row=db.get(PreliminaryEvidenceReport,report_id)
    if not row: raise HTTPException(404,"EVIDENCE_REPORT_NOT_FOUND")
    storage=ObjectStorage(); ttl=timedelta(minutes=settings.artifact_url_ttl_minutes)
    def url(key): return storage.presigned_get(key,ttl) if key else None
    bundle_url=url(row.bundle_object_key) if row.bundle_object_key and has_evidence_permission(identity,EvidencePermission.DOWNLOAD_EVIDENCE_BUNDLE) else None
    return {"web_url":f"/evidence-report.html?case_id={row.case_id}","html_url":url(row.html_object_key),"json_url":url(row.json_object_key),"manifest_url":url(row.manifest_object_key),
            "bundle_url":bundle_url,"expires_minutes":settings.artifact_url_ttl_minutes,
            "permissions":{"view_report":True,"view_raw_evidence":has_evidence_permission(identity,EvidencePermission.VIEW_RAW_EVIDENCE),
                           "download_evidence_bundle":has_evidence_permission(identity,EvidencePermission.DOWNLOAD_EVIDENCE_BUNDLE),
                           "rebuild_report":has_evidence_permission(identity,EvidencePermission.REBUILD_REPORT)}}


@router.post("/reports/evidence/{report_id}/bundle")
def create_bundle(report_id:str,req:EvidenceBundleRequest,db:Session=Depends(get_db),identity=Depends(require_evidence_permission(EvidencePermission.DOWNLOAD_EVIDENCE_BUNDLE))):
    _enabled()
    try:
        storage=ObjectStorage(); artifact=build_evidence_bundle(db,report_id=report_id,profile=req.profile,actor=identity.actor_id,storage=storage)
        download_url=storage.presigned_get(artifact.object_key)
        audit(db,case_id=artifact.case_id,actor=identity.actor_id,event_type="EVIDENCE_BUNDLE_DOWNLOAD_URL_ISSUED",target_type="artifact",target_id=artifact.id,
              detail={"report_id":report_id,"profile":req.profile,"ttl_minutes":settings.artifact_url_ttl_minutes})
        db.commit(); return {"artifact_id":artifact.id,"profile":req.profile,"download_url":download_url,"expires_minutes":settings.artifact_url_ttl_minutes}
    except ValueError as exc:
        db.rollback(); raise HTTPException(404,str(exc))
    except Exception as exc:
        db.rollback(); raise HTTPException(500,f"EVIDENCE_BUNDLE_FAILED:{type(exc).__name__}:{exc}")
