from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import ENGINEER_ROLES, READ_ROLES, get_db, require_roles
from app.contracts.enums import JobStatus
from app.db.models import Job, JobDependency
from app.schemas.jobs import JobDependencyCreate, JobDependencyOut, JobOut
from app.schemas.common import CursorPage
from app.core.pagination import paginate_created
from app.services.idempotency import begin_idempotent, complete_idempotent
from app.services.job_dependencies import create_job_dependency
from app.services.jobs import transition_job

router=APIRouter(prefix='/jobs', tags=['jobs'])


@router.get('/{job_id}', response_model=JobOut)
def get_job(job_id:str, db:Session=Depends(get_db), _identity=Depends(require_roles(*READ_ROLES))):
    row=db.get(Job, job_id)
    if not row: raise HTTPException(404,'JOB_NOT_FOUND')
    return row


@router.get('/by-case/{case_id}', response_model=CursorPage[JobOut])
def case_jobs(case_id:str, limit:int=Query(default=50,ge=1,le=200), cursor:str|None=Query(default=None), db:Session=Depends(get_db), _identity=Depends(require_roles(*READ_ROLES))):
    items,next_cursor,has_more=paginate_created(db,Job,where=(Job.case_id==case_id,),limit=limit,cursor=cursor,descending=False)
    return CursorPage[JobOut](items=[JobOut.model_validate(x) for x in items],next_cursor=next_cursor,has_more=has_more)


@router.get('/{job_id}/dependencies', response_model=list[JobDependencyOut])
def dependencies(job_id:str, db:Session=Depends(get_db), _identity=Depends(require_roles(*READ_ROLES))):
    if not db.get(Job, job_id): raise HTTPException(404,'JOB_NOT_FOUND')
    return list(db.query(JobDependency).filter(JobDependency.job_id==job_id).order_by(JobDependency.created_at.asc()).all())


@router.post('/{job_id}/dependencies', response_model=JobDependencyOut, status_code=201)
def add_dependency(
    job_id:str,
    req:JobDependencyCreate,
    db:Session=Depends(get_db),
    idempotency_key:str|None=Header(default=None,alias='Idempotency-Key'),
    identity=Depends(require_roles(*ENGINEER_ROLES)),
):
    payload=req.model_dump(mode='json')
    handle=begin_idempotent(db,scope=f'POST:/api/v1/jobs/{job_id}/dependencies',key=idempotency_key,payload=payload)
    if handle.replay is not None: return handle.replay
    row=create_job_dependency(db,job_id=job_id,depends_on_job_id=req.depends_on_job_id,policy=req.policy,actor=identity.actor_id)
    response=JobDependencyOut.model_validate(row).model_dump(mode='json')
    complete_idempotent(db,handle,response=response,status_code=201,resource_type='job_dependency',resource_id=row.id)
    db.commit(); db.refresh(row); return row


@router.post('/{job_id}/cancel', response_model=JobOut)
def cancel_job(
    job_id:str,
    db:Session=Depends(get_db),
    idempotency_key:str|None=Header(default=None,alias='Idempotency-Key'),
    identity=Depends(require_roles(*ENGINEER_ROLES)),
):
    row=db.get(Job,job_id)
    if not row: raise HTTPException(404,'JOB_NOT_FOUND')
    handle=begin_idempotent(db,scope=f'POST:/api/v1/jobs/{job_id}/cancel',key=idempotency_key,payload={'job_id':job_id})
    if handle.replay is not None: return handle.replay
    current=JobStatus(row.status)
    if current==JobStatus.PENDING:
        transition_job(db,row,JobStatus.CANCELLED,reason='cancelled_before_start',actor=identity.actor_id,cleanup_verified=True)
    elif current in {JobStatus.RUNNING,JobStatus.WAITING_EVIDENCE,JobStatus.WAITING_USER}:
        transition_job(db,row,JobStatus.CANCEL_REQUESTED,reason='cancel_requested',actor=identity.actor_id)
    # Terminal jobs are returned idempotently.
    response=JobOut.model_validate(row).model_dump(mode='json')
    complete_idempotent(db,handle,response=response,status_code=200,resource_type='job',resource_id=row.id)
    db.commit(); db.refresh(row); return row
