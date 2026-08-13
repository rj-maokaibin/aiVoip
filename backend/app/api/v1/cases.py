from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import ENGINEER_ROLES, READ_ROLES, get_db, require_roles
from app.schemas.cases import CaseCreate, CaseOut, CollectRequest
from app.schemas.common import CursorPage
from app.schemas.jobs import JobOut
from app.schemas.evidence import EvidenceOut
from app.db.models import Case, Evidence
from app.core.pagination import paginate_created
from app.services.cases import create_case, create_collect_job
from app.services.idempotency import begin_idempotent, complete_idempotent
from app.workers.collector_tasks import collect_case

router=APIRouter(prefix='/cases', tags=['cases'])


@router.post('', response_model=CaseOut, status_code=201)
def post_case(
    req:CaseCreate,
    db:Session=Depends(get_db),
    idempotency_key:str|None=Header(default=None,alias='Idempotency-Key'),
    identity=Depends(require_roles(*ENGINEER_ROLES)),
):
    payload=req.model_dump(mode='json')
    handle=begin_idempotent(db,scope='POST:/api/v1/cases',key=idempotency_key,payload=payload)
    if handle.replay is not None: return handle.replay
    actor=identity.actor_id
    row=create_case(db, summary=req.summary, ip=req.ip, ssh_port=req.ssh_port, sn=req.sn, created_by=actor)
    response=CaseOut.model_validate(row).model_dump(mode='json')
    complete_idempotent(db,handle,response=response,status_code=201,resource_type='case',resource_id=row.id); db.commit()
    return row


@router.get('', response_model=CursorPage[CaseOut])
def list_cases(
    limit:int=Query(default=50,ge=1,le=200),
    cursor:str|None=Query(default=None),
    db:Session=Depends(get_db),
    _identity=Depends(require_roles(*READ_ROLES)),
):
    items,next_cursor,has_more=paginate_created(db,Case,limit=limit,cursor=cursor,descending=True)
    return CursorPage[CaseOut](items=[CaseOut.model_validate(x) for x in items],next_cursor=next_cursor,has_more=has_more)


@router.get('/{case_id}', response_model=CaseOut)
def get_case(case_id:str, db:Session=Depends(get_db), _identity=Depends(require_roles(*READ_ROLES))):
    row=db.get(Case, case_id)
    if not row: raise HTTPException(404,'CASE_NOT_FOUND')
    return row


@router.post('/{case_id}/collect', response_model=JobOut, status_code=202)
def collect(
    case_id:str,
    req:CollectRequest,
    db:Session=Depends(get_db),
    idempotency_key:str|None=Header(default=None,alias='Idempotency-Key'),
    _identity=Depends(require_roles(*ENGINEER_ROLES)),
):
    case=db.get(Case, case_id)
    if not case: raise HTTPException(404,'CASE_NOT_FOUND')
    handle=begin_idempotent(db,scope=f'POST:/api/v1/cases/{case_id}/collect',key=idempotency_key,payload=req.model_dump(mode='json'))
    if handle.replay is not None: return handle.replay
    job=create_collect_job(db, case_id, req.profile_id)
    response=JobOut.model_validate(job).model_dump(mode='json')
    complete_idempotent(db,handle,response=response,status_code=202,resource_type='job',resource_id=job.id); db.commit()
    collect_case.apply_async(args=[job.id], queue='collector')
    return job


@router.get('/{case_id}/evidences', response_model=CursorPage[EvidenceOut])
def evidences(
    case_id:str,
    limit:int=Query(default=50,ge=1,le=200),
    cursor:str|None=Query(default=None),
    db:Session=Depends(get_db),
    _identity=Depends(require_roles(*READ_ROLES)),
):
    if not db.get(Case,case_id): raise HTTPException(404,'CASE_NOT_FOUND')
    items,next_cursor,has_more=paginate_created(db,Evidence,where=(Evidence.case_id==case_id,),limit=limit,cursor=cursor,descending=False)
    return CursorPage[EvidenceOut](items=[EvidenceOut.model_validate(x) for x in items],next_cursor=next_cursor,has_more=has_more)
