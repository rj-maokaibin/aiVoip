from __future__ import annotations

from sqlalchemy.orm import Session

from app.contracts.enums import CaseEvent, CaseStatus, JobStatus
from app.core.config import settings
from app.core.ids import new_case_no
from app.db.models import Case, CaseDevice, CaseStateHistory, Job
from app.services.audit import audit
from app.services.case_transitions import CaseTransitionService


def create_case(db:Session, *, summary, ip, ssh_port, sn, created_by=None):
    case=Case(case_no=new_case_no(), summary=summary, status=CaseStatus.NEW.value, created_by=created_by)
    db.add(case); db.flush()
    device=CaseDevice(case_id=case.id, ip=ip, ssh_port=ssh_port, sn=sn, username=settings.ssh_username)
    db.add(device)
    db.add(CaseStateHistory(case_id=case.id, from_status=None, to_status=CaseStatus.NEW.value, reason='case_created'))
    audit(db, case_id=case.id, actor=created_by, event_type='CASE_CREATED', target_type='case', target_id=case.id, detail={'sn':sn,'ip':ip,'ssh_port':ssh_port})
    db.commit(); db.refresh(case); return case


def get_case(db, case_id): return db.get(Case, case_id)


def transition_case(db:Session, case:Case, event:CaseEvent|str, reason:str, *, actor:str|None=None, context:dict|None=None):
    return CaseTransitionService.transition(db,case,event,reason=reason,actor=actor,context=context)


def create_collect_job(db, case_id, profile_id):
    job=Job(case_id=case_id, type='COLLECT_DEVICE', status=JobStatus.PENDING.value, profile_id=profile_id)
    db.add(job); db.flush()
    audit(db, case_id=case_id, event_type='COLLECT_JOB_CREATED', target_type='job', target_id=job.id, detail={'profile_id':profile_id})
    db.commit(); db.refresh(job); return job
