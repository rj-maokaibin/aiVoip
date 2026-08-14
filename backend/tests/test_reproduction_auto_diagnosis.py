"""Reproduction finish -> automatic diagnosis trigger tests.

Closes the gap where a reproduction session that finished (terminal state +
cleanup verified) never handed its captured evidence to the diagnosis worker:
engineers had to click the diagnosis button by hand even though the whole
reproduction ran automatically.
"""
from __future__ import annotations

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import Case, CaseDevice, DiagnosisRun, Job, ReproductionSession


def _engine():
    # StaticPool keeps ONE shared in-memory connection so every SessionLocal()
    # opened by the workers sees the same data (the default SingletonThreadPool
    # would hand each Session a fresh, empty in-memory DB).
    eng = create_engine(
        'sqlite+pysqlite:///:memory:',
        poolclass=StaticPool,
        connect_args={'check_same_thread': False},
    )
    Base.metadata.create_all(eng)
    return eng


def _seed_session(db: Session, *, state='COMPLETED', cleanup_status='CLEANUP_VERIFIED',
                  case_no='C-1', sn='SN-1'):
    case = Case(case_no=case_no, summary='repro auto diag', status='ANALYZING')
    db.add(case); db.flush()
    dev = CaseDevice(case_id=case.id, ip='10.0.0.1', ssh_port=22, sn=sn, username='root')
    db.add(dev); db.flush()
    session = ReproductionSession(
        case_id=case.id, device_id=dev.id, profile_key='VOIP_GENERIC_FULL_CAPTURE',
        profile_version='1.0', profile_checksum='chk', effective_profile_snapshot={},
        state=state, cleanup_status=cleanup_status,
    )
    db.add(session); db.commit(); db.refresh(session)
    return session


def _call(monkeypatch, eng):
    # Patch BOTH the source module attribute and the already-bound name inside
    # reproduction_tasks (module-level `from app.db.session import SessionLocal`).
    import app.db.session as dbs
    monkeypatch.setattr(dbs, 'SessionLocal', lambda: Session(eng))
    import app.workers.reproduction_tasks as rt
    monkeypatch.setattr(rt, 'SessionLocal', lambda: Session(eng), raising=False)


def test_ensure_triggers_diagnosis_for_completed_session(monkeypatch):
    from app.workers.reproduction_tasks import ensure_reproduction_diagnosis
    eng = _engine()
    with Session(eng) as db:
        _call(monkeypatch, eng)
        sess = _seed_session(db)
        dispatched = {}
        def fake_apply_async(args, queue=None):
            dispatched['run_id'] = args[0]
        monkeypatch.setattr('app.workers.diagnosis_tasks.run_diagnosis.apply_async', fake_apply_async, raising=False)
        result = ensure_reproduction_diagnosis(sess.id)
        assert result['status'] == 'TRIGGERED'
        assert result['case_id'] == sess.case_id
        # A diagnosis job + run were created for the case.
        run = db.get(DiagnosisRun, result['run_id'])
        assert run is not None and run.case_id == sess.case_id
        job = db.get(Job, result['job_id'])
        assert job is not None and job.case_id == sess.case_id
        # The diagnosis worker was dispatched with the created run.
        assert dispatched.get('run_id') == result['run_id']


def test_ensure_is_idempotent_when_already_diagnosed(monkeypatch):
    from app.workers.reproduction_tasks import ensure_reproduction_diagnosis
    eng = _engine()
    with Session(eng) as db:
        _call(monkeypatch, eng)
        sess = _seed_session(db)
        # A diagnosis run already exists for the case (e.g. from a prior call).
        existing = DiagnosisRun(case_id=sess.case_id, status='DIAGNOSED', cycle=1,
                                reasoner_name='deterministic', reasoner_version='0.1.0',
                                workflow_version='m4-v1')
        db.add(existing); db.commit(); db.refresh(existing)
        calls = {'n': 0}
        monkeypatch.setattr('app.workers.diagnosis_tasks.run_diagnosis.apply_async',
                            lambda args, queue=None: calls.__setitem__('n', calls['n'] + 1), raising=False)
        result = ensure_reproduction_diagnosis(sess.id)
        assert result['status'] == 'ALREADY_DIAGNOSED'
        assert calls['n'] == 0  # no duplicate dispatch


def test_ensure_skips_non_terminal_session(monkeypatch):
    from app.workers.reproduction_tasks import ensure_reproduction_diagnosis
    eng = _engine()
    with Session(eng) as db:
        _call(monkeypatch, eng)
        sess = _seed_session(db, state='WATCHING', cleanup_status='REQUIRED')
        result = ensure_reproduction_diagnosis(sess.id)
        assert result['status'] == 'NOT_TERMINAL'


def test_ensure_skips_unverified_cleanup(monkeypatch):
    from app.workers.reproduction_tasks import ensure_reproduction_diagnosis
    eng = _engine()
    with Session(eng) as db:
        _call(monkeypatch, eng)
        sess = _seed_session(db, state='COMPLETED', cleanup_status='RUNNING')
        result = ensure_reproduction_diagnosis(sess.id)
        assert result['status'] == 'CLEANUP_NOT_VERIFIED'


def test_ensure_returns_no_session(monkeypatch):
    from app.workers.reproduction_tasks import ensure_reproduction_diagnosis
    eng = _engine()
    with Session(eng) as db:
        _call(monkeypatch, eng)
        result = ensure_reproduction_diagnosis('no-such-session')
        assert result['status'] == 'NO_SESSION'


def test_reconcile_triggers_for_terminal_sessions(monkeypatch):
    from app.workers.reproduction_tasks import reconcile_reproduction
    eng = _engine()
    with Session(eng) as db:
        _call(monkeypatch, eng)
        _seed_session(db, state='COMPLETED', cleanup_status='CLEANUP_VERIFIED')
        _seed_session(db, state='CANCELLED', cleanup_status='CLEANUP_VERIFIED', case_no='C-2', sn='SN-2')
        _seed_session(db, state='WATCHING', cleanup_status='REQUIRED', case_no='C-3', sn='SN-3')
        monkeypatch.setattr('app.workers.diagnosis_tasks.run_diagnosis.apply_async',
                            lambda args, queue=None: None, raising=False)
        # RecoveryReconciler needs real DB plumbing; stub it out.
        class FakeReconciler:
            def reconcile_expired_leases(self, db): return 0
            def retry_failed_cleanups(self, db): return 0
        monkeypatch.setattr('app.workers.reproduction_tasks.RecoveryReconciler', FakeReconciler)
        result = reconcile_reproduction()
        # Only the terminal + cleanup-verified sessions are scanned (C-1, C-2);
        # C-3 (WATCHING) is not terminal and is excluded from the scan.
        assert result['diagnosis_triggered'] == 2  # C-1 and C-2
        # Both terminal sessions now have a diagnosis run.
        runs = list(db.scalars(select(DiagnosisRun)))
        assert len(runs) == 2
