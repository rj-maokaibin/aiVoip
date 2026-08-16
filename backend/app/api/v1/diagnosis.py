from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import ENGINEER_ROLES, READ_ROLES, REVIEWER_ROLES, get_db, require_roles
from app.contracts.enums import CaseEvent, DiagnosisRunStatus, HypothesisState
from app.db.models import AIProposalRecord, AIRecommendationFeedback, Case, CollectionPlan, DiagnosisRun, Hypothesis, HypothesisEvidence, HypothesisRevision
from app.diagnosis.ai_workbench import (
    AIRecommendationFeedbackRequest, EngineeringDraftRequest, build_eval_report, persist_engineering_draft,
    persist_readonly_workbench,
)
from app.diagnosis.snapshot import CaseEvidenceSnapshotBuilder
from app.schemas.diagnosis import CollectionPlanOut, ConfirmHypothesisRequest, DiagnosisRunOut, HypothesisEvidenceOut, HypothesisOut, HypothesisRevisionOut
from app.schemas.jobs import JobOut
from app.services.audit import audit
from app.services.cases import transition_case
from app.services.diagnosis import create_diagnosis_job, latest_diagnosis
from app.services.idempotency import begin_idempotent, complete_idempotent
from app.workers.diagnosis_tasks import run_diagnosis

router=APIRouter(tags=['diagnosis'])


def _ai_record(row: AIProposalRecord) -> dict:
    return {
        'id': row.id, 'case_id': row.case_id, 'diagnosis_run_id': row.diagnosis_run_id,
        'schema_version': row.schema_version, 'intent': row.intent, 'mode': row.mode,
        'status': row.status, 'input_fingerprint': row.input_fingerprint,
        'model_name': row.model_name, 'prompt_version': row.prompt_version,
        'workflow_version': row.workflow_version, 'latency_ms': row.latency_ms,
        'result': row.validated_output_json, 'validation_errors': row.validation_errors or [],
        'diff': row.diff_json or {}, 'gateway_error': row.gateway_error,
        'created_at': row.created_at,
    }


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


@router.get('/cases/{case_id}/ai/records')
def ai_records(case_id:str, mode:str|None=Query(default=None,max_length=32),
               db:Session=Depends(get_db),_identity=Depends(require_roles(*READ_ROLES))):
    if not db.get(Case,case_id): raise HTTPException(404,'CASE_NOT_FOUND')
    stmt=select(AIProposalRecord).where(AIProposalRecord.case_id==case_id)
    if mode: stmt=stmt.where(AIProposalRecord.mode==mode.upper())
    rows=list(db.scalars(stmt.order_by(AIProposalRecord.created_at.desc()).limit(200)))
    return [_ai_record(row) for row in rows]


@router.get('/cases/{case_id}/ai/workbench')
def ai_workbench(case_id:str,db:Session=Depends(get_db),_identity=Depends(require_roles(*READ_ROLES))):
    if not db.get(Case,case_id): raise HTTPException(404,'CASE_NOT_FOUND')
    row=db.scalar(select(AIProposalRecord).where(
        AIProposalRecord.case_id==case_id,AIProposalRecord.mode=='READ_ONLY'
    ).order_by(AIProposalRecord.created_at.desc()).limit(1))
    if not row: raise HTTPException(404,'AI_WORKBENCH_NOT_FOUND')
    return _ai_record(row)


@router.post('/cases/{case_id}/ai/workbench')
def refresh_ai_workbench(case_id:str,db:Session=Depends(get_db),
                         identity=Depends(require_roles(*ENGINEER_ROLES))):
    case=db.get(Case,case_id)
    if not case: raise HTTPException(404,'CASE_NOT_FOUND')
    run=db.scalar(select(DiagnosisRun).where(DiagnosisRun.case_id==case_id)
                  .order_by(DiagnosisRun.created_at.desc()).limit(1))
    baseline=(run.decision_json or {}) if run else {
        'hypotheses':[],'known':[],'unknown':[],'excluded':[],
        'summary':{'headline':'尚无确定性诊断结果'},
    }
    snapshot=CaseEvidenceSnapshotBuilder().build(db,case_id)
    shadow=db.scalar(select(AIProposalRecord).where(
        AIProposalRecord.case_id==case_id,AIProposalRecord.mode=='SHADOW',
        AIProposalRecord.status=='ACCEPTED'
    ).order_by(AIProposalRecord.created_at.desc()).limit(1))
    row=persist_readonly_workbench(db,case_id=case_id,diagnosis_run_id=run.id if run else None,
                                   snapshot=snapshot,baseline=baseline,proposal_record=shadow)
    db.commit(); db.refresh(row)
    return _ai_record(row)


@router.get('/cases/{case_id}/ai/eval')
def ai_eval(case_id:str,db:Session=Depends(get_db),_identity=Depends(require_roles(*READ_ROLES))):
    if not db.get(Case,case_id): raise HTTPException(404,'CASE_NOT_FOUND')
    rows=list(db.scalars(select(AIProposalRecord).where(AIProposalRecord.case_id==case_id)
                         .order_by(AIProposalRecord.created_at.asc())))
    feedback=list(db.scalars(select(AIRecommendationFeedback).where(AIRecommendationFeedback.case_id==case_id)
                             .order_by(AIRecommendationFeedback.created_at.asc())))
    return build_eval_report(rows,feedback)


@router.post('/ai/records/{record_id}/feedback',status_code=201)
def ai_recommendation_feedback(record_id:str,req:AIRecommendationFeedbackRequest,
                               db:Session=Depends(get_db),
                               identity=Depends(require_roles(*ENGINEER_ROLES))):
    proposal=db.get(AIProposalRecord,record_id)
    if not proposal: raise HTTPException(404,'AI_RECORD_NOT_FOUND')
    if proposal.mode!='READ_ONLY': raise HTTPException(409,'AI_FEEDBACK_REQUIRES_READ_ONLY_RECORD')
    row=AIRecommendationFeedback(proposal_id=proposal.id,case_id=proposal.case_id,
                                 item_type=req.item_type,decision=req.decision,
                                 actor=identity.actor_id,reason=req.reason)
    db.add(row); db.flush()
    audit(db,case_id=proposal.case_id,actor=identity.actor_id,event_type='AI_RECOMMENDATION_FEEDBACK',
          target_type='ai_proposal',target_id=proposal.id,
          detail={'item_type':req.item_type,'decision':req.decision,'feedback_id':row.id})
    db.commit(); db.refresh(row)
    return {'id':row.id,'proposal_id':row.proposal_id,'case_id':row.case_id,
            'item_type':row.item_type,'decision':row.decision,'reason':row.reason,
            'actor':row.actor,'created_at':row.created_at}


@router.post('/cases/{case_id}/ai/drafts',status_code=201)
def create_ai_draft(case_id:str,req:EngineeringDraftRequest,db:Session=Depends(get_db),
                    identity=Depends(require_roles(*ENGINEER_ROLES))):
    case=db.get(Case,case_id)
    if not case: raise HTTPException(404,'CASE_NOT_FOUND')
    run=db.scalar(select(DiagnosisRun).where(DiagnosisRun.case_id==case_id)
                  .order_by(DiagnosisRun.created_at.desc()).limit(1))
    baseline=(run.decision_json or {}) if run else {'hypotheses':[],'known':[],'unknown':[],'excluded':[]}
    snapshot=CaseEvidenceSnapshotBuilder().build(db,case_id)
    row=persist_engineering_draft(db,case_id=case_id,request=req,snapshot=snapshot,
                                  baseline=baseline,actor=identity.actor_id)
    db.commit(); db.refresh(row)
    return _ai_record(row)


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
