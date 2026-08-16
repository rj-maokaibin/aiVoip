from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import (
    Case, CaseDevice, FixAction, FixVerificationRun, ReproductionCall,
    ReproductionSession,
)


def _engine():
    engine = create_engine(
        'sqlite+pysqlite:///:memory:', poolclass=StaticPool,
        connect_args={'check_same_thread': False},
    )
    Base.metadata.create_all(engine)
    return engine


def _seed(engine):
    with Session(engine) as db:
        case = Case(case_no='FIX-SCHED-1', summary='修复验证', status='RESOLVING')
        db.add(case); db.flush()
        device = CaseDevice(case_id=case.id, ip='10.0.0.1', ssh_port=22,
                            sn='SN-FIX', username='root')
        db.add(device); db.flush()
        baseline = ReproductionSession(
            case_id=case.id, device_id=device.id, profile_key='VOIP_GENERIC_FULL_CAPTURE',
            profile_version='1', profile_checksum='base', effective_profile_snapshot={},
            state='COMPLETED', cleanup_status='CLEANUP_VERIFIED',
        )
        db.add(baseline); db.flush()
        call = ReproductionCall(case_id=case.id, session_id=baseline.id, call_no=1,
                                status='ANALYZED', verdict='MATCH',
                                quick_analysis_json={'findings': ['ONE_WAY_AUDIO']})
        db.add(call); db.flush()
        fix = FixAction(case_id=case.id, action_type='CONFIG_CHANGE',
                        description='修改配置')
        db.add(fix); db.flush()
        verification = FixVerificationRun(
            case_id=case.id, fix_action_id=fix.id,
            baseline_session_id=baseline.id, baseline_call_id=call.id,
            reproduction_profile_id='VOIP_GENERIC_FULL_CAPTURE',
            target_finding='ONE_WAY_AUDIO', status='PENDING',
        )
        db.add(verification); db.commit()
        return verification.id, case.id


def test_fix_verification_scheduler_creates_one_bound_reproduction(monkeypatch):
    from app.workers.reproduction_tasks import schedule_fix_verification_reproduction
    engine = _engine()
    verification_id, _case_id = _seed(engine)
    monkeypatch.setattr('app.workers.reproduction_tasks.SessionLocal', lambda: Session(engine))
    started = []
    monkeypatch.setattr('app.workers.reproduction_tasks.start_reproduction.apply_async',
                        lambda args, queue=None: started.append(args[0]), raising=False)
    monkeypatch.setattr('app.workers.device_provision_task.sync_case_card.apply_async',
                        lambda args, queue=None: None, raising=False)

    first = schedule_fix_verification_reproduction.run(verification_id)
    second = schedule_fix_verification_reproduction.run(verification_id)

    assert first['status'] == 'SCHEDULED'
    assert second['status'] == 'ALREADY_SCHEDULED'
    assert started == [first['session_id']]
    with Session(engine) as db:
        verification = db.get(FixVerificationRun, verification_id)
        assert verification.status == 'RUNNING'
        assert verification.verification_session_id == first['session_id']
        session = db.get(ReproductionSession, first['session_id'])
        assert session is not None and session.retry_parent_session_id == verification.baseline_session_id


def test_fix_verification_evaluation_binds_latest_analyzed_call(monkeypatch):
    from app.workers.reproduction_tasks import ensure_fix_verification_evaluation
    engine = _engine()
    verification_id, case_id = _seed(engine)
    with Session(engine) as db:
        verification = db.get(FixVerificationRun, verification_id)
        baseline = db.get(ReproductionSession, verification.baseline_session_id)
        current = ReproductionSession(
            case_id=case_id, device_id=baseline.device_id,
            profile_key='VOIP_GENERIC_FULL_CAPTURE', profile_version='1',
            profile_checksum='current', effective_profile_snapshot={},
            state='COMPLETED', cleanup_status='CLEANUP_VERIFIED',
        )
        db.add(current); db.flush()
        call = ReproductionCall(case_id=case_id, session_id=current.id, call_no=1,
                                status='ANALYZED', verdict='NO_MATCH',
                                quick_analysis_json={'findings': [], 'metrics': {}})
        db.add(call)
        verification.verification_session_id = current.id
        verification.status = 'RUNNING'
        db.commit()
        current_id, call_id = current.id, call.id
    monkeypatch.setattr('app.workers.reproduction_tasks.SessionLocal', lambda: Session(engine))
    captured = {}

    def fake_evaluate(_self, db, **kwargs):
        captured.update(kwargs)
        row = kwargs['verification']
        row.status = 'FIX_VERIFIED'
        db.flush()
        return row

    monkeypatch.setattr('app.experiments.fix_verification.FixVerificationService.evaluate',
                        fake_evaluate)
    monkeypatch.setattr('app.workers.device_provision_task.sync_case_card.apply_async',
                        lambda args, queue=None: None, raising=False)
    result = ensure_fix_verification_evaluation(current_id)
    assert result['status'] == 'FIX_VERIFIED'
    assert captured['verification_call_id'] == call_id
    assert captured['business_checks']['target_absent'] is True
