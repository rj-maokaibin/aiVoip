from __future__ import annotations

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.golden_models import GoldenCandidateAssessment  # noqa: F401 - register metadata
from app.db.models import AnalyzerRun, AuditLog, Case, DiagnosisRun, Evidence, Hypothesis, HypothesisEvidence
from app.diagnosis.snapshot import CaseEvidenceSnapshotBuilder
from app.golden.service import GoldenCandidateService


def _engine():
    eng = create_engine(
        'sqlite+pysqlite:///:memory:',
        poolclass=StaticPool,
        connect_args={'check_same_thread': False},
    )
    Base.metadata.create_all(eng)
    return eng


def _case(db: Session, summary: str = '重启后首次拨号偶发丢第一位号码') -> Case:
    row = Case(case_no='GC-1', summary=summary, status='ANALYZING')
    db.add(row); db.flush()
    return row


def _evidence(db: Session, case_id: str, filename: str = 'call_01.pcap') -> Evidence:
    row = Evidence(
        case_id=case_id,
        type='PCAP', source='USER_UPLOAD', kind='RAW', source_scope='CASE', level='L1',
        completeness='COMPLETE', filename=filename, object_key=f'cases/{case_id}/{filename}',
        size_bytes=123, sha256='a' * 64, content_type='application/vnd.tcpdump.pcap',
    )
    db.add(row); db.flush()
    return row


def _analyzer(db: Session, case_id: str, evidence_id: str) -> AnalyzerRun:
    row = AnalyzerRun(
        case_id=case_id,
        analyzer_name='packet',
        analyzer_version='test-v1',
        status='SUCCESS',
        input_evidence_ids=[evidence_id],
        output_evidence_ids=[],
        summary_json={'findings': ['DTMF_PATH_OBSERVED']},
    )
    db.add(row); db.flush()
    return row


def _audit(db: Session, case_id: str, *events: str) -> None:
    for name in events:
        db.add(AuditLog(case_id=case_id, event_type=name, action=name, detail={}))
    db.flush()


def _baseline(db: Session, case_id: str) -> DiagnosisRun:
    row = DiagnosisRun(
        case_id=case_id, status='DIAGNOSED', cycle=1,
        decision_json={'hypotheses': [{'code': 'H_DTMF_PATH'}]},
        summary_json={'headline': '已有确定性诊断候选'},
    )
    db.add(row); db.flush()
    return row


def _confirmed(db: Session, case_id: str, evidence_id: str,
               *, code: str = 'H_DTMF_PATH', title: str = 'DTMF path first mismatch') -> Hypothesis:
    row = Hypothesis(
        case_id=case_id, code=code, title=title, fault_domain='DTMF_PATH', status='CONFIRMED',
        confidence=9000, confirmable=1, confirm_rule='DIRECT_L1',
    )
    db.add(row); db.flush()
    db.add(HypothesisEvidence(
        hypothesis_id=row.id, ref_type='EVIDENCE', ref_id=evidence_id,
        evidence_level='L1', direction='SUPPORT', weight=1000,
    ))
    db.flush()
    return row


def test_empty_case_is_not_eligible(monkeypatch):
    monkeypatch.setattr(CaseEvidenceSnapshotBuilder, 'build', lambda self, db, case_id: {'case': {'id': case_id}})
    eng = _engine()
    with Session(eng) as db:
        case = _case(db)
        result = GoldenCandidateService().assess(db, case.id)
        assert result['status'] == 'NOT_ELIGIBLE'
        assert 'NO_CASE_EVIDENCE' in result['gap_codes']
        assert result['verification_tier'] is None


def test_case_with_evidence_but_no_confirmed_root_is_partial(monkeypatch):
    monkeypatch.setattr(CaseEvidenceSnapshotBuilder, 'build', lambda self, db, case_id: {'case': {'id': case_id}})
    eng = _engine()
    with Session(eng) as db:
        case = _case(db)
        evidence = _evidence(db, case.id)
        _analyzer(db, case.id, evidence.id)
        _baseline(db, case.id)
        _audit(db, case.id, 'CASE_CREATED', 'EVIDENCE_UPLOADED', 'ANALYZER_COMPLETED', 'DIAGNOSIS_CYCLE')
        result = GoldenCandidateService().assess(db, case.id)
        assert result['status'] == 'PARTIAL_GOLDEN'
        assert 'ROOT_CAUSE_NOT_CONFIRMED' in result['gap_codes']
        assert any(x['code'] == 'CONFIRM_ROOT_CAUSE' for x in result['next_steps'])


def test_confirmed_grounded_case_becomes_golden_ready_tier_b(monkeypatch):
    monkeypatch.setattr(CaseEvidenceSnapshotBuilder, 'build', lambda self, db, case_id: {'case': {'id': case_id}})
    eng = _engine()
    with Session(eng) as db:
        case = _case(db)
        evidence = _evidence(db, case.id)
        _analyzer(db, case.id, evidence.id)
        _baseline(db, case.id)
        _confirmed(db, case.id, evidence.id)
        _audit(db, case.id, 'CASE_CREATED', 'EVIDENCE_UPLOADED', 'ANALYZER_COMPLETED', 'DIAGNOSIS_CYCLE', 'HYPOTHESIS_CONFIRMED')
        result = GoldenCandidateService().assess(db, case.id)
        assert result['status'] == 'GOLDEN_READY'
        assert result['verification_tier'] == 'B'
        assert result['signals']['root_cause_confirmed'] is True
        assert result['signals']['direct_l1_support'] is True
        assert result['signals']['successful_analyzer_count'] == 1
        assert result['signals']['audit_coverage_complete'] is True
        assert result['signals']['answer_leakage_risk'] is False


def test_confirmed_case_without_analyzer_stays_candidate(monkeypatch):
    monkeypatch.setattr(CaseEvidenceSnapshotBuilder, 'build', lambda self, db, case_id: {'case': {'id': case_id}})
    eng = _engine()
    with Session(eng) as db:
        case = _case(db)
        evidence = _evidence(db, case.id)
        _baseline(db, case.id)
        _confirmed(db, case.id, evidence.id)
        _audit(db, case.id, 'CASE_CREATED', 'EVIDENCE_UPLOADED', 'DIAGNOSIS_CYCLE', 'HYPOTHESIS_CONFIRMED')
        result = GoldenCandidateService().assess(db, case.id)
        assert result['status'] == 'GOLDEN_CANDIDATE'
        assert 'NO_SUCCESSFUL_ANALYZER' in result['gap_codes']
        assert any(x['code'] == 'RUN_DETERMINISTIC_ANALYZERS' for x in result['next_steps'])


def test_answer_leakage_blocks_ready(monkeypatch):
    monkeypatch.setattr(CaseEvidenceSnapshotBuilder, 'build', lambda self, db, case_id: {'case': {'id': case_id}})
    eng = _engine()
    with Session(eng) as db:
        case = _case(db, '问题已确认，根因 H_DTMF_PATH 导致首次拨号丢号')
        evidence = _evidence(db, case.id)
        _analyzer(db, case.id, evidence.id)
        _baseline(db, case.id)
        _confirmed(db, case.id, evidence.id)
        _audit(db, case.id, 'CASE_CREATED', 'EVIDENCE_UPLOADED', 'ANALYZER_COMPLETED', 'DIAGNOSIS_CYCLE', 'HYPOTHESIS_CONFIRMED')
        result = GoldenCandidateService().assess(db, case.id)
        assert result['status'] == 'GOLDEN_CANDIDATE'
        assert 'ANSWER_LEAKAGE_RISK' in result['blocker_codes']
        assert result['signals']['answer_leakage_risk'] is True
        assert any(x['code'] == 'REMOVE_ANSWER_LEAKAGE' for x in result['next_steps'])


def test_refresh_persists_latest_state_and_transition_audit(monkeypatch):
    monkeypatch.setattr(CaseEvidenceSnapshotBuilder, 'build', lambda self, db, case_id: {'case': {'id': case_id}})
    eng = _engine()
    with Session(eng) as db:
        case = _case(db)
        _audit(db, case.id, 'CASE_CREATED')
        row = GoldenCandidateService().refresh(db, case.id, actor='test')
        db.commit()
        assert row.status == 'NOT_ELIGIBLE'
        stored = db.scalar(select(GoldenCandidateAssessment).where(GoldenCandidateAssessment.case_id == case.id))
        assert stored is not None
        transition = db.scalar(select(AuditLog).where(
            AuditLog.case_id == case.id,
            AuditLog.event_type == 'GOLDEN_CANDIDATE_STATE_CHANGED',
        ))
        assert transition is not None
        assert (transition.after_json or {})['status'] == 'NOT_ELIGIBLE'
