from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.contracts.enums import CollectionPlanStatus, DiagnosisRunStatus, JobStatus, normalize_hypothesis_state
from app.db.models import CollectionPlan, DiagnosisRun, Hypothesis, HypothesisEvidence, HypothesisRevision, Job
from app.services.audit import audit


def create_diagnosis_job(db:Session, *, case_id:str) -> tuple[Job,DiagnosisRun]:
    job=Job(case_id=case_id,type='AI_DIAGNOSIS',status=JobStatus.PENDING.value)
    db.add(job); db.flush()
    run=DiagnosisRun(case_id=case_id,job_id=job.id,status=DiagnosisRunStatus.PENDING.value,cycle=0,reasoner_name='deterministic',reasoner_version='0.1.0',workflow_version='m4-v1')
    db.add(run); db.flush()
    audit(db,case_id=case_id,event_type='DIAGNOSIS_STARTED',target_type='diagnosis_run',target_id=run.id,
          detail={'job_id':job.id,'reasoner':run.reasoner_name,'version':run.reasoner_version})
    db.commit(); db.refresh(job); db.refresh(run); return job,run


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
    db.flush(); return rows


def create_plan(db:Session, run:DiagnosisRun, decision) -> CollectionPlan|None:
    if not decision.plan: return None
    goal=decision.summary.get('headline') or '补充诊断证据'
    row=CollectionPlan(case_id=run.case_id,diagnosis_run_id=run.id,cycle=run.cycle,status=CollectionPlanStatus.PROPOSED.value,goal=goal,
                       actions_json=[a.to_dict() for a in sorted(decision.plan,key=lambda x:x.priority)],execution_job_ids=[])
    db.add(row); db.flush(); return row


def latest_diagnosis(db:Session,case_id:str) -> DiagnosisRun|None:
    return db.scalar(select(DiagnosisRun).where(DiagnosisRun.case_id==case_id).order_by(DiagnosisRun.created_at.desc()))
