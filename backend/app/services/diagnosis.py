from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.contracts.enums import ActorType, CollectionPlanStatus, DiagnosisRunStatus, JobStatus, normalize_hypothesis_state
from app.core.config import settings
from app.db.models import CollectionPlan, DiagnosisRun, Hypothesis, HypothesisEvidence, HypothesisRevision, Job
from app.diagnosis.ai_cycle import AIDiagnosticCycleService
from app.services.audit import audit


def create_diagnosis_job(db:Session, *, case_id:str) -> tuple[Job,DiagnosisRun]:
    job=Job(case_id=case_id,type='AI_DIAGNOSIS',status=JobStatus.PENDING.value)
    db.add(job); db.flush()
    run=DiagnosisRun(case_id=case_id,job_id=job.id,status=DiagnosisRunStatus.PENDING.value,cycle=0,reasoner_name='deterministic',reasoner_version='0.1.0',workflow_version='m4-v1')
    db.add(run); db.flush()
    audit(db,case_id=case_id,event_type='DIAGNOSIS_STARTED',target_type='diagnosis_run',target_id=run.id,
          detail={'job_id':job.id,'reasoner':run.reasoner_name,'version':run.reasoner_version})
    db.commit(); db.refresh(job); db.refresh(run); return job,run


def _run_ai2_sidecar(db: Session, run: DiagnosisRun, decision):
    """Best-effort AI2 cycle after deterministic hypothesis persistence.

    The savepoint makes AI2 non-blocking: any model/schema/sidecar failure rolls back
    only AI2 writes and cannot invalidate the deterministic diagnosis transaction.
    SHADOW/SUGGEST are the only stages attached here; CONTROLLED_PLANNER remains on
    the separate promotion + Policy/Orchestrator path.
    """
    if not settings.ai_diagnostic_loop_enabled:
        return None
    if str(settings.ai_promotion_stage or 'OFF').upper() not in {'SHADOW', 'SUGGEST'}:
        return None
    try:
        baseline = dict(decision.to_dict())
        baseline['diagnosis_run_id'] = run.id
        with db.begin_nested():
            execution = AIDiagnosticCycleService().run_next(
                db,
                case_id=run.case_id,
                actor='diagnosis-worker',
                deterministic_baseline=baseline,
            )
        decision.summary = {
            **decision.summary,
            'ai2_cycle_id': execution.row.id,
            'ai2_cycle_stage': execution.row.runtime_stage,
            'ai2_cycle_status': execution.row.status,
            'ai2_continue_recommendation': execution.row.continue_recommendation,
            'ai2_registered_next_action': (execution.row.next_action_json or {}).get('registered_id'),
            'ai2_dispatch_attempted': False,
            'ai2_formal_result_changed': False,
        }
        return execution
    except Exception as exc:
        audit(
            db,
            case_id=run.case_id,
            actor='diagnosis-worker',
            actor_type=ActorType.AI,
            event_type='AI_DIAGNOSTIC_CYCLE_FAILED',
            target_type='diagnosis_run',
            target_id=run.id,
            detail={
                'schema_version':'ai-diagnostic-cycle-failure-v1',
                'error_code':type(exc).__name__,
                'error_message':str(exc)[:500],
                'deterministic_transaction_preserved':True,
                'dispatch_attempted':False,
                'formal_result_changed':False,
            },
        )
        return None


def persist_decision(db:Session, run:DiagnosisRun, decision) -> list[Hypothesis]:
    """Persist a current projection plus immutable hypothesis revisions.

    `Hypothesis` remains the stable identity/current projection for existing API clients.
    Every diagnosis cycle appends a `HypothesisRevision` and evidence links scoped to
    that revision. Historical evidence is never deleted or overwritten.
    """
    rows=[]
    for proposal in decision.hypotheses:
        row=db.scalar(select(Hypothesis).where(Hypothesis.case_id==run.case_id,Hypothesis.code==proposal.code))
        if not row:
            row=Hypothesis(case_id=run.case_id,diagnosis_run_id=run.id,code=proposal.code,title=proposal.title,fault_domain=proposal.fault_domain)
            db.add(row); db.flush()
        max_rev=db.scalar(select(func.max(HypothesisRevision.revision_no)).where(HypothesisRevision.hypothesis_id==row.id)) or 0
        state=normalize_hypothesis_state(proposal.status).value
        confidence=int(round(max(0,min(1,proposal.confidence))*10000))
        revision=HypothesisRevision(
            hypothesis_id=row.id, diagnosis_run_id=run.id, revision_no=max_rev+1,
            supersedes_revision_id=row.current_revision_id, title=proposal.title, fault_domain=proposal.fault_domain,
            status=state, confidence=confidence, rationale=proposal.rationale,
            confirmable=1 if proposal.confirmable else 0, confirm_rule=proposal.confirm_rule,
        )
        db.add(revision); db.flush()
        for ref in proposal.evidence:
            db.add(HypothesisEvidence(
                hypothesis_id=row.id,hypothesis_revision_id=revision.id,ref_type=ref.ref_type,ref_id=ref.ref_id,
                evidence_level=ref.level,direction=ref.direction,weight=int(round(ref.weight*1000)),
                rationale=ref.rationale,details_json=ref.details,
            ))
        # Update only the explicit current projection; immutable revisions remain the audit source of truth.
        row.diagnosis_run_id=run.id; row.title=proposal.title; row.fault_domain=proposal.fault_domain; row.status=state
        row.confidence=confidence; row.rationale=proposal.rationale; row.confirmable=1 if proposal.confirmable else 0
        row.confirm_rule=proposal.confirm_rule; row.current_revision_id=revision.id
        rows.append(row)
    db.flush()
    _run_ai2_sidecar(db, run, decision)
    return rows


def create_plan(db:Session, run:DiagnosisRun, decision) -> CollectionPlan|None:
    if not decision.plan: return None
    goal=decision.summary.get('headline') or '补充诊断证据'
    row=CollectionPlan(case_id=run.case_id,diagnosis_run_id=run.id,cycle=run.cycle,status=CollectionPlanStatus.PROPOSED.value,goal=goal,
                       actions_json=[a.to_dict() for a in sorted(decision.plan,key=lambda x:x.priority)],execution_job_ids=[])
    db.add(row); db.flush(); return row


def latest_diagnosis(db:Session,case_id:str) -> DiagnosisRun|None:
    return db.scalar(select(DiagnosisRun).where(DiagnosisRun.case_id==case_id).order_by(DiagnosisRun.created_at.desc()))