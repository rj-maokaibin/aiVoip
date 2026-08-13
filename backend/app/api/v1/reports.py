from datetime import timedelta
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.api.deps import ENGINEER_ROLES, READ_ROLES, get_db, require_roles
from app.core.config import settings
from app.db.models import Case, DiagnosisReport
from app.integrations.storage import ObjectStorage
from app.reports.diagnosis_report import generate_report
from app.schemas.reports import DiagnosisReportOut, ReportGenerateRequest
from app.schemas.common import CursorPage
from app.core.pagination import paginate_created
from app.services.audit import audit
from app.services.idempotency import begin_idempotent, complete_idempotent

router=APIRouter(tags=['reports'])

@router.post('/cases/{case_id}/reports/diagnosis',response_model=DiagnosisReportOut)
def generate(case_id:str,req:ReportGenerateRequest,db:Session=Depends(get_db),idempotency_key:str|None=Header(default=None,alias='Idempotency-Key'),identity=Depends(require_roles(*ENGINEER_ROLES))):
    if not db.get(Case,case_id): raise HTTPException(404,'CASE_NOT_FOUND')
    handle=begin_idempotent(db,scope=f'POST:/api/v1/cases/{case_id}/reports/diagnosis',key=idempotency_key,payload={'case_id':case_id})
    if handle.replay is not None: return handle.replay
    try:
        row,_=generate_report(db,case_id,actor=identity.actor_id)
        audit(db,case_id=case_id,actor=identity.actor_id,event_type='DIAGNOSIS_REPORT_GENERATED',target_type='diagnosis_report',target_id=row.id)
        response=DiagnosisReportOut.model_validate(row).model_dump(mode='json')
        complete_idempotent(db,handle,response=response,status_code=200,resource_type='diagnosis_report',resource_id=row.id)
        db.commit(); db.refresh(row); return row
    except Exception as exc: db.rollback(); raise HTTPException(500,f'REPORT_GENERATION_FAILED:{type(exc).__name__}:{exc}')

@router.get('/cases/{case_id}/reports',response_model=CursorPage[DiagnosisReportOut])
def reports(case_id:str,limit:int=Query(default=50,ge=1,le=200),cursor:str|None=Query(default=None),db:Session=Depends(get_db),_identity=Depends(require_roles(*READ_ROLES))):
    items,next_cursor,has_more=paginate_created(db,DiagnosisReport,where=(DiagnosisReport.case_id==case_id,),limit=limit,cursor=cursor,descending=True)
    return CursorPage[DiagnosisReportOut](items=[DiagnosisReportOut.model_validate(x) for x in items],next_cursor=next_cursor,has_more=has_more)

@router.get('/reports/{report_id}/links')
def report_links(report_id:str,db:Session=Depends(get_db),_identity=Depends(require_roles(*READ_ROLES))):
    row=db.get(DiagnosisReport,report_id)
    if not row: raise HTTPException(404,'REPORT_NOT_FOUND')
    storage=ObjectStorage(); ttl=timedelta(minutes=settings.artifact_url_ttl_minutes)
    return {'html_url':storage.presigned_get(row.html_object_key,ttl),'json_url':storage.presigned_get(row.json_object_key,ttl),'expires_minutes':settings.artifact_url_ttl_minutes}
