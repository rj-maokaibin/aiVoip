from __future__ import annotations

from fastapi import APIRouter, Depends, Header
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_permissions
from app.contracts.enums import PermissionName
from app.core.errors import AppError
from app.db.models import (
    CausalAssessment, DiagnosticExperiment, DiagnosticQuestion, EnvironmentComparison,
    ExperimentRun, FixAction, FixVerificationRun,
)
from app.experiments.fix_verification import FixVerificationService
from app.experiments.orchestrator import DiagnosticExperimentOrchestrator
from app.experiments.profile import ExperimentProfileRegistry
from app.reproduction.question_graph import DiagnosticQuestionGraph, DiagnosticQuestionRegistry
from app.schemas.experiments import (
    CausalAssessmentOut, DiagnosticExperimentOut, DiagnosticQuestionOut, DiagnosticQuestionTemplateOut,
    EnvironmentComparisonOut, ExperimentAttachResultRequest, ExperimentCreateRequest, ExperimentProfileOut,
    ExperimentRunOut, ExperimentStartReproductionRequest, FixActionCreateRequest, FixActionOut, FixVerificationCreateRequest,
    FixVerificationEvaluateRequest, FixVerificationOut, QuestionAnswerRequest,
)
from app.schemas.reproduction import ReproductionSessionOut
from app.services.idempotency import begin_idempotent, complete_idempotent

router=APIRouter(tags=['diagnostic-experiments'])


@router.get('/diagnostic-question-templates',response_model=list[DiagnosticQuestionTemplateOut])
def list_question_templates(_identity=Depends(require_permissions(PermissionName.DIAGNOSIS_READ))):
    result=[]
    for item in DiagnosticQuestionRegistry().list():
        result.append(DiagnosticQuestionTemplateOut(
            id=item.id,version=item.version,level=item.level.value,title=item.title,priority=item.priority,
            information_gain=item.information_gain,required_evidence=item.required_evidence.model_dump(mode='json'),
            next_questions=item.next_questions,next_by_route=item.next_by_route,experiment_profiles=item.experiment_profiles,checksum=item.checksum,
        ))
    return result


@router.get('/cases/{case_id}/diagnostic-questions',response_model=list[DiagnosticQuestionOut])
def list_questions(case_id:str,db:Session=Depends(get_db),_identity=Depends(require_permissions(PermissionName.DIAGNOSIS_READ))):
    return list(db.scalars(select(DiagnosticQuestion).where(DiagnosticQuestion.case_id==case_id).order_by(DiagnosticQuestion.created_at)))


@router.post('/diagnostic-questions/{question_id}/answer',response_model=list[DiagnosticQuestionOut])
def answer_question(question_id:str,req:QuestionAnswerRequest,db:Session=Depends(get_db),identity=Depends(require_permissions(PermissionName.DIAGNOSIS_RUN))):
    row=db.get(DiagnosticQuestion,question_id)
    if not row: raise AppError('DIAGNOSTIC_QUESTION_NOT_FOUND')
    created=DiagnosticQuestionGraph().answer(db,question=row,answer=req.answer,evidence_refs=req.evidence_refs,route=req.route,actor=identity.actor_id)
    db.commit()
    return created


@router.get('/experiment-profiles',response_model=list[ExperimentProfileOut])
def list_experiment_profiles(_identity=Depends(require_permissions(PermissionName.EXPERIMENT_READ))):
    result=[]
    for loaded in ExperimentProfileRegistry().list():
        d=loaded.definition
        result.append(ExperimentProfileOut(
            id=d.id,name=d.name,version=d.version,checksum=loaded.checksum,hypothesis_codes=d.hypothesis_codes,
            reproduction_profile_id=d.reproduction_profile_id,independent_variable=d.independent_variable,target_finding=d.target_finding,
            confirmation_policy=d.confirmation_policy.value,sequence=[x.value for x in d.sequence],external_action_required=d.external_action_required,
            external_action_instructions=d.external_action_instructions,expected_change_paths=d.expected_change_paths,must_equal_paths=d.must_equal_paths,
            soft_drift_paths=d.soft_drift_paths,
        ))
    return result


@router.post('/cases/{case_id}/experiments',response_model=DiagnosticExperimentOut,status_code=201)
def create_experiment(case_id:str,req:ExperimentCreateRequest,db:Session=Depends(get_db),idempotency_key:str|None=Header(default=None,alias='Idempotency-Key'),identity=Depends(require_permissions(PermissionName.EXPERIMENT_CONTROL))):
    payload=req.model_dump(mode='json')
    handle=begin_idempotent(db,scope=f'POST:/api/v1/cases/{case_id}/experiments',key=idempotency_key,payload=payload)
    if handle.replay is not None: return handle.replay
    row=DiagnosticExperimentOrchestrator().create_experiment(db,case_id=case_id,profile_id=req.profile_id,hypothesis_id=req.hypothesis_id,question_id=req.question_id,actor=identity.actor_id)
    response=DiagnosticExperimentOut.model_validate(row).model_dump(mode='json')
    complete_idempotent(db,handle,response=response,status_code=201,resource_type='diagnostic_experiment',resource_id=row.id)
    db.commit(); return row



@router.get('/cases/{case_id}/experiments',response_model=list[DiagnosticExperimentOut])
def list_case_experiments(case_id:str,db:Session=Depends(get_db),_identity=Depends(require_permissions(PermissionName.EXPERIMENT_READ))):
    return list(db.scalars(select(DiagnosticExperiment).where(DiagnosticExperiment.case_id==case_id).order_by(DiagnosticExperiment.created_at.desc())))


@router.get('/experiments/{experiment_id}',response_model=DiagnosticExperimentOut)
def get_experiment(experiment_id:str,db:Session=Depends(get_db),_identity=Depends(require_permissions(PermissionName.EXPERIMENT_READ))):
    row=db.get(DiagnosticExperiment,experiment_id)
    if not row: raise AppError('EXPERIMENT_NOT_FOUND')
    return row


@router.get('/experiments/{experiment_id}/runs',response_model=list[ExperimentRunOut])
def list_experiment_runs(experiment_id:str,db:Session=Depends(get_db),_identity=Depends(require_permissions(PermissionName.EXPERIMENT_READ))):
    if not db.get(DiagnosticExperiment,experiment_id): raise AppError('EXPERIMENT_NOT_FOUND')
    return list(db.scalars(select(ExperimentRun).where(ExperimentRun.experiment_id==experiment_id).order_by(ExperimentRun.run_no)))


@router.post('/experiments/{experiment_id}/runs/next',response_model=ExperimentRunOut|None)
def plan_next_run(experiment_id:str,db:Session=Depends(get_db),identity=Depends(require_permissions(PermissionName.EXPERIMENT_CONTROL))):
    exp=db.get(DiagnosticExperiment,experiment_id)
    if not exp: raise AppError('EXPERIMENT_NOT_FOUND')
    row=DiagnosticExperimentOrchestrator().plan_next_run(db,experiment=exp,actor=identity.actor_id)
    db.commit(); return row


@router.post('/experiment-runs/{run_id}/external-action-completed',response_model=ExperimentRunOut)
def external_action_completed(run_id:str,db:Session=Depends(get_db),identity=Depends(require_permissions(PermissionName.EXPERIMENT_CONTROL))):
    row=db.get(ExperimentRun,run_id)
    if not row: raise AppError('EXPERIMENT_RUN_NOT_FOUND')
    row=DiagnosticExperimentOrchestrator().complete_external_action(db,run=row,actor=identity.actor_id)
    db.commit(); return row


@router.post('/experiment-runs/{run_id}/start-reproduction',response_model=ReproductionSessionOut,status_code=202)
def start_experiment_reproduction(run_id:str,req:ExperimentStartReproductionRequest,db:Session=Depends(get_db),identity=Depends(require_permissions(PermissionName.EXPERIMENT_CONTROL))):
    row=db.get(ExperimentRun,run_id)
    if not row: raise AppError('EXPERIMENT_RUN_NOT_FOUND')
    session=DiagnosticExperimentOrchestrator().start_reproduction(
        db,run=row,external_state=req.external_state,call_context=req.call_context,
        environment_overrides=req.environment_overrides,actor=identity.actor_id,
    )
    db.commit(); return session


@router.post('/experiment-runs/{run_id}/attach-result',response_model=CausalAssessmentOut)
def attach_experiment_result(run_id:str,req:ExperimentAttachResultRequest,db:Session=Depends(get_db),identity=Depends(require_permissions(PermissionName.EXPERIMENT_CONTROL))):
    row=db.get(ExperimentRun,run_id)
    if not row: raise AppError('EXPERIMENT_RUN_NOT_FOUND')
    assessment=DiagnosticExperimentOrchestrator().attach_result(db,run=row,session_id=req.session_id,call_id=req.call_id,
        external_state=req.external_state,call_context=req.call_context,environment_overrides=req.environment_overrides,
        hard_contradictions=req.hard_contradictions,actor=identity.actor_id)
    db.commit(); return assessment


@router.get('/experiments/{experiment_id}/environment-comparisons',response_model=list[EnvironmentComparisonOut])
def list_environment_comparisons(experiment_id:str,db:Session=Depends(get_db),_identity=Depends(require_permissions(PermissionName.EXPERIMENT_READ))):
    return list(db.scalars(select(EnvironmentComparison).where(EnvironmentComparison.experiment_id==experiment_id).order_by(EnvironmentComparison.created_at)))


@router.get('/experiments/{experiment_id}/causal-assessments',response_model=list[CausalAssessmentOut])
def list_causal_assessments(experiment_id:str,db:Session=Depends(get_db),_identity=Depends(require_permissions(PermissionName.EXPERIMENT_READ))):
    return list(db.scalars(select(CausalAssessment).where(CausalAssessment.experiment_id==experiment_id).order_by(CausalAssessment.created_at)))



@router.get('/cases/{case_id}/fix-actions',response_model=list[FixActionOut])
def list_fix_actions(case_id:str,db:Session=Depends(get_db),_identity=Depends(require_permissions(PermissionName.FIX_READ))):
    return list(db.scalars(select(FixAction).where(FixAction.case_id==case_id).order_by(FixAction.created_at.desc())))


@router.get('/cases/{case_id}/fix-verifications',response_model=list[FixVerificationOut])
def list_case_fix_verifications(case_id:str,db:Session=Depends(get_db),_identity=Depends(require_permissions(PermissionName.FIX_READ))):
    return list(db.scalars(select(FixVerificationRun).where(FixVerificationRun.case_id==case_id).order_by(FixVerificationRun.created_at.desc())))


@router.post('/cases/{case_id}/fix-actions',response_model=FixActionOut,status_code=201)
def create_fix_action(case_id:str,req:FixActionCreateRequest,db:Session=Depends(get_db),idempotency_key:str|None=Header(default=None,alias='Idempotency-Key'),identity=Depends(require_permissions(PermissionName.FIX_CONTROL))):
    payload=req.model_dump(mode='json')
    handle=begin_idempotent(db,scope=f'POST:/api/v1/cases/{case_id}/fix-actions',key=idempotency_key,payload=payload)
    if handle.replay is not None: return handle.replay
    row=FixVerificationService().create_fix_action(db,case_id=case_id,action_type=req.action_type,description=req.description,hypothesis_id=req.hypothesis_id,
        experiment_id=req.experiment_id,version_before=req.version_before,version_after=req.version_after,metadata=req.metadata,actor=identity.actor_id)
    response=FixActionOut.model_validate(row).model_dump(mode='json')
    complete_idempotent(db,handle,response=response,status_code=201,resource_type='fix_action',resource_id=row.id)
    db.commit(); return row


@router.post('/fix-actions/{fix_action_id}/verifications',response_model=FixVerificationOut,status_code=201)
def create_fix_verification(fix_action_id:str,req:FixVerificationCreateRequest,db:Session=Depends(get_db),idempotency_key:str|None=Header(default=None,alias='Idempotency-Key'),identity=Depends(require_permissions(PermissionName.FIX_CONTROL))):
    payload=req.model_dump(mode='json')
    handle=begin_idempotent(db,scope=f'POST:/api/v1/fix-actions/{fix_action_id}/verifications',key=idempotency_key,payload=payload)
    if handle.replay is not None: return handle.replay
    row=FixVerificationService().create_verification(db,fix_action_id=fix_action_id,baseline_session_id=req.baseline_session_id,baseline_call_id=req.baseline_call_id,
        target_finding=req.target_finding,reproduction_profile_id=req.reproduction_profile_id,required_calls=req.required_calls,max_calls=req.max_calls)
    response=FixVerificationOut.model_validate(row).model_dump(mode='json')
    complete_idempotent(db,handle,response=response,status_code=201,resource_type='fix_verification',resource_id=row.id)
    db.commit(); return row


@router.post('/fix-verifications/{verification_id}/evaluate',response_model=FixVerificationOut)
def evaluate_fix_verification(verification_id:str,req:FixVerificationEvaluateRequest,db:Session=Depends(get_db),idempotency_key:str|None=Header(default=None,alias='Idempotency-Key'),identity=Depends(require_permissions(PermissionName.FIX_CONTROL))):
    row=db.get(FixVerificationRun,verification_id)
    if not row: raise AppError('FIX_VERIFICATION_NOT_FOUND')
    payload=req.model_dump(mode='json')
    handle=begin_idempotent(db,scope=f'POST:/api/v1/fix-verifications/{verification_id}/evaluate',key=idempotency_key,payload=payload)
    if handle.replay is not None: return handle.replay
    row=FixVerificationService().evaluate(db,verification=row,verification_session_id=req.verification_session_id,verification_call_id=req.verification_call_id,
        baseline_environment=req.baseline_environment,verification_environment=req.verification_environment,business_checks=req.business_checks,
        new_blocking_findings=req.new_blocking_findings,actor=identity.actor_id)
    response=FixVerificationOut.model_validate(row).model_dump(mode='json')
    complete_idempotent(db,handle,response=response,status_code=200,resource_type='fix_verification',resource_id=row.id)
    db.commit(); return row


@router.get('/fix-verifications/{verification_id}',response_model=FixVerificationOut)
def get_fix_verification(verification_id:str,db:Session=Depends(get_db),_identity=Depends(require_permissions(PermissionName.FIX_READ))):
    row=db.get(FixVerificationRun,verification_id)
    if not row: raise AppError('FIX_VERIFICATION_NOT_FOUND')
    return row
