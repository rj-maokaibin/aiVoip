from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.db.base import Base
from app.db.evidence_retention_models import EvidenceRetentionState
from app.db.golden_models import GoldenCandidateAssessment
from app.db.models import AuditLog, Case, Evidence
from app.services.evidence_retention import ensure_retention_state, expire_due_evidence, lock_evidence, unlock_evidence


def _engine():
    eng = create_engine('sqlite+pysqlite:///:memory:', poolclass=StaticPool, connect_args={'check_same_thread': False})
    Base.metadata.create_all(eng)
    return eng


def _utc_naive(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _case(db: Session, no: str = 'RET-1') -> Case:
    row = Case(case_no=no, summary='retention test', status='ANALYZING')
    db.add(row); db.flush(); return row


def _evidence(db: Session, case_id: str, *, created_at: datetime, kind: str = 'RAW') -> Evidence:
    row = Evidence(
        case_id=case_id, type='PCAP', source='TEST', kind=kind, source_scope='CASE', level='L1', completeness='COMPLETE',
        filename='call.pcap', object_key=f'cases/{case_id}/call.pcap', size_bytes=100, sha256='a'*64,
        content_type='application/vnd.tcpdump.pcap', created_at=created_at,
    )
    db.add(row); db.flush(); return row


def test_raw_evidence_defaults_to_90_day_policy():
    eng = _engine()
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with Session(eng) as db:
        case = _case(db)
        ev = _evidence(db, case.id, created_at=now)
        state = ensure_retention_state(db, ev)
        assert state.policy == 'STANDARD_90D'
        assert _utc_naive(state.retain_until) == _utc_naive(now + timedelta(days=settings.evidence_retention_raw_days))
        assert state.status == 'ACTIVE'


def test_golden_candidate_raw_evidence_is_long_term_exempt():
    eng = _engine()
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with Session(eng) as db:
        case = _case(db, 'RET-GOLDEN')
        db.add(GoldenCandidateAssessment(case_id=case.id, status='GOLDEN_CANDIDATE'))
        db.flush()
        ev = _evidence(db, case.id, created_at=now)
        state = ensure_retention_state(db, ev)
        assert state.policy == 'LONG_TERM_GOLDEN'
        assert state.golden_exempt is True
        assert state.retain_until is None


def test_late_golden_promotion_is_refreshed_before_expiry():
    eng = _engine()
    old = datetime(2025, 1, 1, tzinfo=timezone.utc)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with Session(eng) as db:
        case = _case(db, 'RET-LATE-GOLDEN')
        ev = _evidence(db, case.id, created_at=old)
        state = ensure_retention_state(db, ev)
        assert state.policy == 'STANDARD_90D'
        db.add(GoldenCandidateAssessment(case_id=case.id, status='GOLDEN_CANDIDATE'))
        db.flush()
        result = expire_due_evidence(db, now=now, storage_delete=False)
        db.refresh(state)
        assert result['golden_refreshed'] == 1
        assert result['expired'] == 0
        assert state.golden_exempt is True
        assert state.policy == 'LONG_TERM_GOLDEN'
        assert state.retain_until is None
        assert ev.completeness == 'COMPLETE'


def test_manual_lock_and_unlock_are_audited():
    eng = _engine()
    now = datetime.now(timezone.utc)
    with Session(eng) as db:
        case = _case(db, 'RET-LOCK')
        ev = _evidence(db, case.id, created_at=now)
        locked = lock_evidence(db, evidence_id=ev.id, actor='reviewer', reason='customer escalation')
        assert locked.status == 'LOCKED'
        assert locked.retain_until is None
        unlocked = unlock_evidence(db, evidence_id=ev.id, actor='reviewer', reason='case closed')
        assert unlocked.status == 'ACTIVE'
        assert unlocked.policy == 'STANDARD_90D'
        events = list(db.scalars(select(AuditLog).where(AuditLog.case_id == case.id).order_by(AuditLog.created_at.asc())))
        assert [x.event_type for x in events][-2:] == ['EVIDENCE_RETENTION_LOCKED', 'EVIDENCE_RETENTION_UNLOCKED']


def test_expiry_deletes_payload_semantics_but_keeps_metadata_row():
    eng = _engine()
    old = datetime(2025, 1, 1, tzinfo=timezone.utc)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with Session(eng) as db:
        case = _case(db, 'RET-EXPIRE')
        ev = _evidence(db, case.id, created_at=old)
        ensure_retention_state(db, ev)
        result = expire_due_evidence(db, now=now, storage_delete=False)
        assert result['expired'] == 1
        assert db.get(Evidence, ev.id) is not None
        db.refresh(ev)
        assert ev.completeness == 'UNAVAILABLE'
        assert (ev.metadata_json or {})['retention_status'] == 'EXPIRED'
        state = db.scalar(select(EvidenceRetentionState).where(EvidenceRetentionState.evidence_id == ev.id))
        assert state is not None and state.status == 'EXPIRED'
        assert _utc_naive(state.expired_at) == _utc_naive(now)
