from __future__ import annotations

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
import pytest

from app.api.deps import AuthIdentity, require_permissions
from app.contracts.enums import (
    CaseEvent, CaseStatus, DependencyPolicy, JobStatus, PermissionName, UserRole,
)
from app.core.errors import AppError
from app.core.pagination import paginate_created
from app.db.base import Base
from app.db.models import AuditLog, Case, CaseStateHistory, EventOutbox, Job, JobStateHistory
from app.services.audit import audit
from app.services.case_transitions import CaseTransitionService
from app.services.job_dependencies import create_job_dependency
from app.services.jobs import transition_job


def _engine():
    eng=create_engine('sqlite+pysqlite:///:memory:')
    Base.metadata.create_all(eng)
    return eng


def _case(db:Session,no:str)->Case:
    row=Case(case_no=no,summary='a2 contract test',status=CaseStatus.NEW.value)
    db.add(row); db.flush(); return row


def test_permission_registry_is_server_side_and_role_bounded():
    dep=require_permissions(PermissionName.JOB_CONTROL)
    with pytest.raises(AppError) as exc:
        dep(identity=AuthIdentity('viewer',UserRole.VIEWER,True))
    assert exc.value.code=='PERMISSION_DENIED'
    assert dep(identity=AuthIdentity('engineer',UserRole.ENGINEER,True)).actor_id=='engineer'


def test_job_dependency_is_enforced_before_running_and_history_is_append_only():
    eng=_engine()
    with Session(eng) as db:
        case=_case(db,'A2-1')
        upstream=Job(case_id=case.id,type='UP',status=JobStatus.PENDING.value)
        child=Job(case_id=case.id,type='DOWN',status=JobStatus.PENDING.value)
        db.add_all([upstream,child]); db.flush()
        create_job_dependency(db,job_id=child.id,depends_on_job_id=upstream.id,policy=DependencyPolicy.WAIT_ALL_SUCCESS,actor='eng')
        with pytest.raises(AppError) as exc:
            transition_job(db,child,JobStatus.RUNNING,reason='too_early')
        assert exc.value.code=='DEPENDENCY_NOT_SATISFIED'
        transition_job(db,upstream,JobStatus.RUNNING,reason='start',actor='worker')
        transition_job(db,upstream,JobStatus.SUCCESS,reason='done',actor='worker')
        transition_job(db,child,JobStatus.RUNNING,reason='dependencies_ready',actor='worker')
        histories=list(db.scalars(select(JobStateHistory).where(JobStateHistory.job_id==upstream.id).order_by(JobStateHistory.created_at)))
        assert [(x.from_status,x.to_status) for x in histories]==[
            (JobStatus.PENDING.value,JobStatus.RUNNING.value),
            (JobStatus.RUNNING.value,JobStatus.SUCCESS.value),
        ]


def test_dependency_rejects_cross_case_and_cycle():
    eng=_engine()
    with Session(eng) as db:
        c1=_case(db,'A2-2'); c2=_case(db,'A2-3')
        a=Job(case_id=c1.id,type='A',status=JobStatus.PENDING.value)
        b=Job(case_id=c1.id,type='B',status=JobStatus.PENDING.value)
        other=Job(case_id=c2.id,type='C',status=JobStatus.PENDING.value)
        db.add_all([a,b,other]); db.flush()
        create_job_dependency(db,job_id=b.id,depends_on_job_id=a.id,policy=DependencyPolicy.WAIT_ALL_SUCCESS)
        with pytest.raises(AppError) as exc:
            create_job_dependency(db,job_id=a.id,depends_on_job_id=b.id,policy=DependencyPolicy.WAIT_ALL_SUCCESS)
        assert exc.value.code=='JOB_DEPENDENCY_CYCLE'
        with pytest.raises(AppError) as exc:
            create_job_dependency(db,job_id=a.id,depends_on_job_id=other.id,policy=DependencyPolicy.WAIT_ALL_SUCCESS)
        assert exc.value.code=='JOB_DEPENDENCY_CROSS_CASE'


def test_case_history_records_event_actor_context():
    eng=_engine()
    with Session(eng) as db:
        case=_case(db,'A2-4')
        CaseTransitionService.transition(db,case,CaseEvent.TRIAGE_STARTED,reason='triage',actor='alice',context={'origin':'api'})
        row=db.scalar(select(CaseStateHistory).where(CaseStateHistory.case_id==case.id))
        assert row.event==CaseEvent.TRIAGE_STARTED.value
        assert row.actor=='alice'
        assert row.context_json=={'origin':'api'}


def test_normalized_audit_fields_and_registered_sse_event():
    eng=_engine()
    with Session(eng) as db:
        case=_case(db,'A2-5')
        row=audit(db,case_id=case.id,actor='alice',event_type='CASE_STATE_CHANGED',action='CASE_TRANSITION',target_type='case',target_id=case.id,
                  before={'status':'NEW'},after={'status':'TRIAGING'},reason='triage',trace_id='trace-1',detail={'event':'TRIAGE_STARTED'})
        db.flush()
        assert row.action=='CASE_TRANSITION' and row.before_json['status']=='NEW' and row.after_json['status']=='TRIAGING'
        assert row.reason=='triage' and row.trace_id=='trace-1'
        out=db.scalar(select(EventOutbox).where(EventOutbox.event_type=='CASE_STATE_CHANGED'))
        assert out is not None and out.case_id==case.id


def test_cursor_pagination_is_stable_and_opaque():
    eng=_engine()
    with Session(eng) as db:
        for i in range(5): _case(db,f'A2-P-{i}')
        db.flush()
        page1,cursor,more=paginate_created(db,Case,limit=2,descending=False)
        assert len(page1)==2 and cursor and more
        page2,cursor2,more2=paginate_created(db,Case,limit=2,cursor=cursor,descending=False)
        assert len(page2)==2 and cursor2 and more2
        page3,cursor3,more3=paginate_created(db,Case,limit=2,cursor=cursor2,descending=False)
        assert len(page3)==1 and cursor3 is None and not more3
        assert len({x.id for x in page1+page2+page3})==5
