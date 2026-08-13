from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_permissions
from app.contracts.enums import PermissionName
from app.core.config import settings
from app.core.errors import AppError
from app.db.models import Case, ReproductionAttempt, ReproductionCall, ReproductionSession
from app.reproduction.bundle import build_reproduction_evidence_bundle
from app.reproduction.orchestrator import ReproductionOrchestrator
from app.reproduction.profile import ReproductionProfileRegistry
from app.schemas.reproduction import (
    ReproductionAttemptOut, ReproductionBundleOut, ReproductionCallOut, ReproductionCreate,
    ReproductionProfileOut, ReproductionSessionOut,
)
from app.services.idempotency import begin_idempotent, complete_idempotent
from app.workers.reproduction_tasks import cancel_reproduction, start_reproduction

router=APIRouter(tags=['reproduction'])


@router.get('/reproduction-profiles',response_model=list[ReproductionProfileOut])
def list_profiles(_identity=Depends(require_permissions(PermissionName.REPRODUCTION_READ))):
    result=[]
    for loaded in ReproductionProfileRegistry().list():
        d=loaded.definition
        result.append(ReproductionProfileOut(
            id=d.id,name=d.name,version=d.version,checksum=loaded.checksum,symptom_classes=d.symptom_classes,
            end_policy=d.end_policy.value,max_calls=d.max_calls,stages=[x.stage.value for x in d.stages],
        ))
    return result


@router.post('/cases/{case_id}/reproductions',response_model=ReproductionSessionOut,status_code=202)
def create_reproduction(
    case_id: str,
    req: ReproductionCreate,
    db: Session=Depends(get_db),
    idempotency_key: str|None=Header(default=None,alias='Idempotency-Key'),
    identity=Depends(require_permissions(PermissionName.REPRODUCTION_CONTROL)),
):
    if not db.get(Case,case_id): raise AppError('CASE_NOT_FOUND')
    payload=req.model_dump(mode='json')
    handle=begin_idempotent(db,scope=f'POST:/api/v1/cases/{case_id}/reproductions',key=idempotency_key,payload=payload)
    if handle.replay is not None: return handle.replay
    row=ReproductionOrchestrator().create_session(
        db,case_id=case_id,profile_id=req.profile_id,symptom_class=req.symptom_class,device_id=req.device_id,actor=identity.actor_id)
    response=ReproductionSessionOut.model_validate(row).model_dump(mode='json')
    complete_idempotent(db,handle,response=response,status_code=202,resource_type='reproduction_session',resource_id=row.id)
    db.commit()
    # Autonomous main flow: creation schedules ARM immediately. In production the EC-02 adapter must replace mock mode.
    start_reproduction.apply_async(args=[row.id],queue='reproduction')
    return row




@router.get('/cases/{case_id}/reproductions',response_model=list[ReproductionSessionOut])
def list_case_reproductions(case_id: str,db:Session=Depends(get_db),_identity=Depends(require_permissions(PermissionName.REPRODUCTION_READ))):
    if not db.get(Case,case_id): raise AppError('CASE_NOT_FOUND')
    return list(db.scalars(select(ReproductionSession).where(ReproductionSession.case_id==case_id).order_by(ReproductionSession.created_at.desc())))


@router.get('/reproductions/{session_id}',response_model=ReproductionSessionOut)
def get_reproduction(session_id: str,db:Session=Depends(get_db),_identity=Depends(require_permissions(PermissionName.REPRODUCTION_READ))):
    row=db.get(ReproductionSession,session_id)
    if not row: raise AppError('REPRODUCTION_NOT_FOUND')
    return row


@router.get('/reproductions/{session_id}/attempts',response_model=list[ReproductionAttemptOut])
def get_attempts(session_id: str,db:Session=Depends(get_db),_identity=Depends(require_permissions(PermissionName.REPRODUCTION_READ))):
    if not db.get(ReproductionSession,session_id): raise AppError('REPRODUCTION_NOT_FOUND')
    return list(db.scalars(select(ReproductionAttempt).where(ReproductionAttempt.session_id==session_id).order_by(ReproductionAttempt.attempt_no)))


@router.get('/reproductions/{session_id}/calls',response_model=list[ReproductionCallOut])
def get_calls(session_id: str,db:Session=Depends(get_db),_identity=Depends(require_permissions(PermissionName.REPRODUCTION_READ))):
    if not db.get(ReproductionSession,session_id): raise AppError('REPRODUCTION_NOT_FOUND')
    return list(db.scalars(select(ReproductionCall).where(ReproductionCall.session_id==session_id).order_by(ReproductionCall.call_no)))


@router.get('/reproductions/{session_id}/bundle',response_model=ReproductionBundleOut)
def get_bundle(session_id: str,db:Session=Depends(get_db),_identity=Depends(require_permissions(PermissionName.REPRODUCTION_READ))):
    row=db.get(ReproductionSession,session_id)
    if not row: raise AppError('REPRODUCTION_NOT_FOUND')
    return build_reproduction_evidence_bundle(db,row)


@router.post('/reproductions/{session_id}/stop',response_model=ReproductionSessionOut,status_code=202)
def stop_reproduction(
    session_id:str,db:Session=Depends(get_db),identity=Depends(require_permissions(PermissionName.REPRODUCTION_CONTROL)),
):
    row=db.get(ReproductionSession,session_id)
    if not row: raise AppError('REPRODUCTION_NOT_FOUND')
    cancel_reproduction.apply_async(args=[row.id],queue='reproduction')
    return row
