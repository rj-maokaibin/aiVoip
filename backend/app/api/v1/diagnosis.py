from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import ENGINEER_ROLES, READ_ROLES, REVIEWER_ROLES, get_db, require_roles
from app.contracts.enums import CaseEvent, DiagnosisRunStatus, HypothesisState
from app.db.models import Case, CollectionPlan, DiagnosisRun, Hypothesis, HypothesisEvidence, HypothesisRevision
from app.schemas.diagnosis import CollectionPlanOut, ConfirmHypothesisRequest, DiagnosisRunOut, HypothesisEvidenceOut, HypothesisOut, HypothesisRevisionOut
from app.schemas.jobs import JobOut
from app.services.audit import audit
from app.services.cases import transition_case
from app.services.diagnosis import create_diagnosis_job, latest_diagnosis
from app.services.idempotency import begin_idempotent, complete_idempotent
from app.workers.diagnosis_tasks import run_diagnosis

router=APIRouter(tags=['diagnosis'])


@router.post('/cases/{case_id}/diagnosis/start',response_model=JobOut,status_code=202)
def start(
    case_id:str,
    db:Session=Depends(get_db),
    idempotency_key:str|None=Header(default=None,alias='Idempotency-Key'),
    _identity=Depends(require_roles(*ENGINEER_ROLES)),
):
    case=db.get(Case,case_id)
    if not case: raise HTTPException(404,'CASE_NOT_FOUND')
    handle=begin_idempotent(db,scope=f'POST:/api/v1/cases/{case_id}/diagnosis/start',key=idempotency_key,payload={'case_id':case_id})
    if handle.replay is not None: return handle.replay
    active=db.scalar(select(DiagnosisRun).where(DiagnosisRun.case_id==case_id,DiagnosisRun.status.in_([
        DiagnosisRunStatus.PENDING.value, DiagnosisRunStatus.ANALYZING.value, DiagnosisRunStatus.WAITING_EVIDENCE.value,
    ])).order_by(DiagnosisRun.created_at.desc()))
    if active and active.job_id:
        from app.db.models import Job
        job=db.get(Job,active.job_id)
        if job:
            response=JobOut.model_validate(job).model_dump(mode='json')
            complete_idempotent(db,handle,response=response,status_code=202,resource_type='job',resource_id=job.id); db.commit()
            return job
    job,run=create_diagnosis_job(db,case_id=case_id)
    response=JobOut.model_validate(job).model_dump(mode='json')
    complete_idempotent(db,handle,response=response,status_code=202,resource_type='job',resource_id=job.id); db.commit()
    run_diagnosis.apply_async(args=[run.id],queue='diagnosis')
    return job


@router.get('/cases/{case_id}/diagnosis/latest',response_model=DiagnosisRunOut)
def latest(case_id:str,db:Session=Depends(get_db),_identity=Depends(require_roles(*READ_ROLES))):
    row=latest_diagnosis(db,case_id)
    if not row: raise HTTPException(404,'DIAGNOSIS_NOT_FOUND')
    return row


@router.get('/diagnosis-runs/{run_id}',response_model=DiagnosisRunOut)
def get_run(run_id:str,db:Session=Depends(get_db),_identity=Depends(require_roles(*READ_ROLES))):
    row=db.get(DiagnosisRun,run_id)
    if not row: raise HTTPException(404,'DIAGNOSIS_NOT_FOUND')
    return row


@router.get('/diagnosis-runs/{run_id}/hypotheses',response_model=list[HypothesisOut])
def hypotheses(run_id:str,db:Session=Depends(get_db),_identity=Depends(require_roles(*READ_ROLES))):
    run=db.get(DiagnosisRun,run_id)
    if not run: raise HTTPException(404,'DIAGNOSIS_NOT_FOUND')
    return list(db.scalars(select(Hypothesis).where(Hypothesis.case_id==run.case_id).order_by(Hypothesis.confidence.desc())))


@router.get('/hypotheses/{hypothesis_id}/revisions',response_model=list[HypothesisRevisionOut])
def hypothesis_revisions(hypothesis_id:str,db:Session=Depends(get_db),_identity=Depends(require_roles(*READ_ROLES))):
    if not db.get(Hypothesis,hypothesis_id): raise HTTPException(404,'HYPOTHESIS_NOT_FOUND')
    return list(db.scalars(select(HypothesisRevision).where(HypothesisRevision.hypothesis_id==hypothesis_id).order_by(HypothesisRevision.revision_no.asc())))


@router.get('/hypotheses/{hypothesis_id}/evidence',response_model=list[HypothesisEvidenceOut])
def hypothesis_evidence(
    hypothesis_id:str,
    include_history:bool=Query(default=False),
    db:Session=Depends(get_db),
    _identity=Depends(require_roles(*READ_ROLES)),
):
    h=db.get(Hypothesis,hypothesis_id)
    if not h: raise HTTPException(404,'HYPOTHESIS_NOT_FOUND')
    stmt=select(HypothesisEvidence).where(HypothesisEvidence.hypothesis_id==hypothesis_id)
    if not include_history and h.current_revision_id:
        stmt=stmt.where(HypothesisEvidence.hypothesis_revision_id==h.current_revision_id)
    return list(db.scalars(stmt.order_by(HypothesisEvidence.created_at.asc())))


@router.get('/diagnosis-runs/{run_id}/plans',response_model=list[CollectionPlanOut])
def plans(run_id:str,db:Session=Depends(get_db),_identity=Depends(require_roles(*READ_ROLES))):
    if not db.get(DiagnosisRun,run_id): raise HTTPException(404,'DIAGNOSIS_NOT_FOUND')
    return list(db.scalars(select(CollectionPlan).where(CollectionPlan.diagnosis_run_id==run_id).order_by(CollectionPlan.cycle.asc())))


@router.post('/hypotheses/{hypothesis_id}/confirm',response_model=HypothesisOut)
def confirm(
    hypothesis_id:str,
    req:ConfirmHypothesisRequest,
    db:Session=Depends(get_db),
    idempotency_key:str|None=Header(default=None,alias='Idempotency-Key'),
    identity=Depends(require_roles(*REVIEWER_ROLES)),
):
    handle=begin_idempotent(db,scope=f'POST:/api/v1/hypotheses/{hypothesis_id}/confirm',key=idempotency_key,payload={'hypothesis_id':hypothesis_id,'note':req.note})
    if handle.replay is not None: return handle.replay
    h=db.get(Hypothesis,hypothesis_id)
    if not h: raise HTTPException(404,'HYPOTHESIS_NOT_FOUND')
    if not h.confirmable: raise HTTPException(409,'HYPOTHESIS_NOT_CONFIRMABLE')
    if not h.confirm_rule: raise HTTPException(409,'CONFIRM_RULE_REQUIRED')
    stmt=select(HypothesisEvidence).where(HypothesisEvidence.hypothesis_id==h.id)
    if h.current_revision_id:
        stmt=stmt.where(HypothesisEvidence.hypothesis_revision_id==h.current_revision_id)
    refs=list(db.scalars(stmt))
    if not any(x.evidence_level=='L1' and x.direction=='SUPPORT' and x.ref_type in {'ANALYZER_RUN','EVIDENCE'} for x in refs): raise HTTPException(409,'DIRECT_EVIDENCE_REQUIRED')
    if any(x.direction=='CONTRADICT' and x.evidence_level in {'L1','L2'} for x in refs): raise HTTPException(409,'KEY_CONTRADICTION_EXISTS')

    max_rev=db.scalar(select(func.max(HypothesisRevision.revision_no)).where(HypothesisRevision.hypothesis_id==h.id)) or 0
    revision=HypothesisRevision(
        hypothesis_id=h.id,diagnosis_run_id=h.diagnosis_run_id,revision_no=max_rev+1,supersedes_revision_id=h.current_revision_id,
        title=h.title,fault_domain=h.fault_domain,status=HypothesisState.CONFIRMED.value,confidence=h.confidence,
        rationale=(h.rationale or '') + (f'\nHuman confirmation: {req.note}' if req.note else ''),confirmable=h.confirmable,confirm_rule=h.confirm_rule,
    )
    db.add(revision); db.flush()
    for ref in refs:
        db.add(HypothesisEvidence(
            hypothesis_id=h.id,hypothesis_revision_id=revision.id,ref_type=ref.ref_type,ref_id=ref.ref_id,evidence_level=ref.evidence_level,
            direction=ref.direction,weight=ref.weight,rationale=ref.rationale,details_json=ref.details_json,
        ))
    h.status=HypothesisState.CONFIRMED.value; h.current_revision_id=revision.id
    case=db.get(Case,h.case_id); transition_case(db,case,CaseEvent.ROOT_CAUSE_CONFIRMED,'human_confirmed_hypothesis',actor=identity.actor_id)
    audit(db,case_id=h.case_id,actor=identity.actor_id,event_type='HYPOTHESIS_CONFIRMED',target_type='hypothesis',target_id=h.id,
          detail={'code':h.code,'note':req.note,'confirm_rule':h.confirm_rule,'revision_id':revision.id})
    response=HypothesisOut.model_validate(h).model_dump(mode='json')
    complete_idempotent(db,handle,response=response,status_code=200,resource_type='hypothesis',resource_id=h.id)
    db.commit(); db.refresh(h); return h
