import json
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import ENGINEER_ROLES, READ_ROLES, get_db, require_roles
from app.contracts.enums import RunStatus
from app.db.models import AnalyzerRun, Evidence
from app.integrations.storage import ObjectStorage
from app.schemas.analyzers import AnalyzerRunOut
from app.schemas.jobs import JobOut
from app.schemas.common import CursorPage
from app.core.pagination import paginate_created
from app.services.analysis import create_packet_analysis_job, create_pcm_analysis_job, create_media_analysis_job
from app.services.idempotency import begin_idempotent, complete_idempotent
from app.workers.packet_tasks import analyze_evidence
from app.workers.pcm_tasks import analyze_pcm_evidence
from app.workers.media_tasks import analyze_media_evidence

router=APIRouter(tags=['analyzers'])


def _begin_job(db, *, scope, key, payload, creator, dispatcher):
    handle=begin_idempotent(db,scope=scope,key=key,payload=payload)
    if handle.replay is not None: return handle.replay, None
    job=creator()
    response=JobOut.model_validate(job).model_dump(mode='json')
    complete_idempotent(db,handle,response=response,status_code=202,resource_type='job',resource_id=job.id); db.commit()
    dispatcher(job)
    return job, job


@router.post('/evidences/{evidence_id}/analyze/packet', response_model=JobOut, status_code=202)
def analyze_packet(evidence_id:str, db:Session=Depends(get_db), idempotency_key:str|None=Header(default=None,alias='Idempotency-Key'), _identity=Depends(require_roles(*ENGINEER_ROLES))):
    evidence=db.get(Evidence, evidence_id)
    if not evidence: raise HTTPException(404,'EVIDENCE_NOT_FOUND')
    result,_=_begin_job(db,scope=f'POST:/api/v1/evidences/{evidence_id}/analyze/packet',key=idempotency_key,payload={'evidence_id':evidence_id},
        creator=lambda:create_packet_analysis_job(db,case_id=evidence.case_id,evidence_id=evidence.id),
        dispatcher=lambda job:analyze_evidence.apply_async(args=[job.id,evidence.id],queue='packet'))
    return result


@router.post('/evidences/{evidence_id}/analyze/pcm', response_model=JobOut, status_code=202)
def analyze_pcm(evidence_id:str, profile_id:str='ruijie_aim_diag_v1', db:Session=Depends(get_db), idempotency_key:str|None=Header(default=None,alias='Idempotency-Key'), _identity=Depends(require_roles(*ENGINEER_ROLES))):
    evidence=db.get(Evidence, evidence_id)
    if not evidence: raise HTTPException(404,'EVIDENCE_NOT_FOUND')
    result,_=_begin_job(db,scope=f'POST:/api/v1/evidences/{evidence_id}/analyze/pcm',key=idempotency_key,payload={'evidence_id':evidence_id,'profile_id':profile_id},
        creator=lambda:create_pcm_analysis_job(db,case_id=evidence.case_id,evidence_id=evidence.id,profile_id=profile_id),
        dispatcher=lambda job:analyze_pcm_evidence.apply_async(args=[job.id,evidence.id,profile_id],queue='pcm'))
    return result


@router.post('/evidences/{evidence_id}/analyze/media', response_model=JobOut, status_code=202)
def analyze_media(evidence_id:str, profile_id:str='ruijie_aim_diag_v1', db:Session=Depends(get_db), idempotency_key:str|None=Header(default=None,alias='Idempotency-Key'), _identity=Depends(require_roles(*ENGINEER_ROLES))):
    evidence=db.get(Evidence, evidence_id)
    if not evidence: raise HTTPException(404,'EVIDENCE_NOT_FOUND')
    result,_=_begin_job(db,scope=f'POST:/api/v1/evidences/{evidence_id}/analyze/media',key=idempotency_key,payload={'evidence_id':evidence_id,'profile_id':profile_id},
        creator=lambda:create_media_analysis_job(db,case_id=evidence.case_id,evidence_id=evidence.id,profile_id=profile_id),
        dispatcher=lambda job:analyze_media_evidence.apply_async(args=[job.id,evidence.id,profile_id],queue='media'))
    return result


@router.get('/cases/{case_id}/analyzer-runs', response_model=CursorPage[AnalyzerRunOut])
def case_runs(case_id:str, limit:int=Query(default=50,ge=1,le=200), cursor:str|None=Query(default=None), db:Session=Depends(get_db), _identity=Depends(require_roles(*READ_ROLES))):
    items,next_cursor,has_more=paginate_created(db,AnalyzerRun,where=(AnalyzerRun.case_id==case_id,),limit=limit,cursor=cursor,descending=False)
    return CursorPage[AnalyzerRunOut](items=[AnalyzerRunOut.model_validate(x) for x in items],next_cursor=next_cursor,has_more=has_more)


@router.get('/jobs/{job_id}/analyzer-runs', response_model=CursorPage[AnalyzerRunOut])
def job_runs(job_id:str, limit:int=Query(default=50,ge=1,le=200), cursor:str|None=Query(default=None), db:Session=Depends(get_db), _identity=Depends(require_roles(*READ_ROLES))):
    items,next_cursor,has_more=paginate_created(db,AnalyzerRun,where=(AnalyzerRun.job_id==job_id,),limit=limit,cursor=cursor,descending=False)
    return CursorPage[AnalyzerRunOut](items=[AnalyzerRunOut.model_validate(x) for x in items],next_cursor=next_cursor,has_more=has_more)


@router.get('/analyzer-runs/{run_id}', response_model=AnalyzerRunOut)
def get_run(run_id:str, db:Session=Depends(get_db), _identity=Depends(require_roles(*READ_ROLES))):
    run=db.get(AnalyzerRun, run_id)
    if not run: raise HTTPException(404,'ANALYZER_RUN_NOT_FOUND')
    return run


@router.get('/analyzer-runs/{run_id}/result')
def get_result(run_id:str, db:Session=Depends(get_db), _identity=Depends(require_roles(*READ_ROLES))):
    run=db.get(AnalyzerRun, run_id)
    if not run: raise HTTPException(404,'ANALYZER_RUN_NOT_FOUND')
    if run.status not in {RunStatus.SUCCESS.value,RunStatus.PARTIAL_SUCCESS.value} or not run.result_object_key:
        raise HTTPException(409,'ANALYZER_RESULT_NOT_READY')
    return json.loads(ObjectStorage().get_bytes(run.result_object_key))
