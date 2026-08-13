from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.api.deps import AuthIdentity, require_roles
from app.contracts.enums import (
    CaseEvent,
    CaseStatus,
    EvidenceCompleteness,
    EvidenceKind,
    EvidenceLevel,
    EvidenceScope,
    HypothesisState,
    IdempotencyStatus,
    JobStatus,
    RuleCategory,
    UserRole,
)
from app.core.errors import AppError
from app.db.base import Base
from app.db.models import (
    Case,
    DiagnosisRun,
    Evidence,
    EvidenceRelation,
    EventOutbox,
    HypothesisRevision,
    IdempotencyRecord,
    Job,
)
from app.diagnosis.types import DiagnosisDecision, EvidenceRef, HypothesisProposal
from app.rules.compiler import compile_rule
from app.rules.engine import RuleEngine
from app.services.case_transitions import CaseTransitionService
from app.services.diagnosis import persist_decision
from app.services.evidence import create_evidence
from app.services.idempotency import begin_idempotent, complete_idempotent, fail_idempotent
from app.services.jobs import transition_job


def _engine():
    eng = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(eng)
    return eng


def _case(db: Session, *, status=CaseStatus.NEW.value, no="C-1") -> Case:
    row = Case(case_no=no, summary="contract test", status=status)
    db.add(row)
    db.flush()
    return row


def _raw_evidence(db: Session, case_id: str, *, metadata=None, completeness=EvidenceCompleteness.COMPLETE):
    return create_evidence(
        db,
        case_id=case_id,
        evidence_type="TEST_RAW",
        source="TEST",
        filename="raw.bin",
        object_key=f"cases/{case_id}/raw.bin",
        size_bytes=3,
        sha256="a" * 64,
        kind=EvidenceKind.RAW,
        scope=EvidenceScope.CASE,
        level=EvidenceLevel.L1,
        completeness=completeness,
        metadata=metadata or {},
        producer_type="TEST",
        producer_version="1",
    )


def test_case_transition_is_event_driven_and_emits_outbox():
    eng = _engine()
    with Session(eng) as db:
        case = _case(db)
        CaseTransitionService.transition(db, case, CaseEvent.TRIAGE_STARTED, reason="test", actor="alice")
        db.commit()
        assert case.status == CaseStatus.TRIAGING.value
        event = db.scalar(select(EventOutbox).where(EventOutbox.event_type == "CASE_STATE_CHANGED"))
        assert event is not None and event.case_id == case.id


def test_case_transition_rejects_arbitrary_jump():
    eng = _engine()
    with Session(eng) as db:
        case = _case(db)
        with pytest.raises(AppError) as exc:
            CaseTransitionService.transition(db, case, CaseEvent.FIX_VERIFIED, reason="invalid")
        assert exc.value.code == "CASE_TRANSITION_NOT_ALLOWED"


def test_fix_verified_requires_complete_same_case_verified_evidence():
    eng = _engine()
    with Session(eng) as db:
        case = _case(db, status=CaseStatus.RESOLVING.value)
        bad = _raw_evidence(db, case.id, metadata={"result": "FIX_FAILED"})
        with pytest.raises(AppError) as exc:
            CaseTransitionService.transition(
                db, case, CaseEvent.FIX_VERIFIED, reason="bad", context={"fix_verification_evidence_id": bad.id}
            )
        assert exc.value.code == "FIX_VERIFICATION_EVIDENCE_REQUIRED"
        good = _raw_evidence(db, case.id, metadata={"result": "FIX_VERIFIED"})
        CaseTransitionService.transition(
            db, case, CaseEvent.FIX_VERIFIED, reason="verified", context={"fix_verification_evidence_id": good.id}
        )
        assert case.status == CaseStatus.RESOLVED.value


def test_job_cancelled_requires_cleanup_verification():
    eng = _engine()
    with Session(eng) as db:
        case = _case(db)
        job = Job(case_id=case.id, type="TEST", status=JobStatus.CANCEL_REQUESTED.value)
        db.add(job); db.flush()
        with pytest.raises(AppError) as exc:
            transition_job(db, job, JobStatus.CANCELLED, reason="test")
        assert exc.value.code == "CANCEL_CLEANUP_REQUIRED"
        transition_job(db, job, JobStatus.CANCELLED, reason="test", cleanup_verified=True)
        assert job.status == JobStatus.CANCELLED.value


def test_idempotency_replays_and_rejects_payload_change():
    eng = _engine()
    with Session(eng) as db:
        h = begin_idempotent(db, scope="POST:/x", key="k1", payload={"a": 1})
        complete_idempotent(db, h, response={"id": "1"}, status_code=201)
        db.commit()
        replay = begin_idempotent(db, scope="POST:/x", key="k1", payload={"a": 1})
        assert replay.replay == {"id": "1"}
        with pytest.raises(AppError) as exc:
            begin_idempotent(db, scope="POST:/x", key="k1", payload={"a": 2})
        assert exc.value.code == "IDEMPOTENCY_KEY_REUSED"


def test_failed_or_expired_idempotency_can_retry_same_request():
    eng = _engine()
    with Session(eng) as db:
        h = begin_idempotent(db, scope="POST:/x", key="retry", payload={"a": 1})
        fail_idempotent(db, h); db.commit()
        retried = begin_idempotent(db, scope="POST:/x", key="retry", payload={"a": 1})
        assert retried.record is not None and retried.record.status == IdempotencyStatus.IN_PROGRESS.value
        retried.record.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
        retried2 = begin_idempotent(db, scope="POST:/x", key="retry", payload={"a": 1})
        assert retried2.record.id == retried.record.id


def test_derived_evidence_requires_lineage_and_records_relation():
    eng = _engine()
    with Session(eng) as db:
        case = _case(db)
        parent = _raw_evidence(db, case.id)
        with pytest.raises(AppError) as exc:
            create_evidence(
                db, case_id=case.id, evidence_type="FINDING", source="ANALYZER", filename="x.json",
                object_key="x", size_bytes=1, sha256="b"*64, kind=EvidenceKind.DERIVED,
                scope=EvidenceScope.CALL, level=EvidenceLevel.L1, completeness=EvidenceCompleteness.COMPLETE,
            )
        assert exc.value.code == "EVIDENCE_LINEAGE_REQUIRED"
        child = create_evidence(
            db, case_id=case.id, evidence_type="FINDING", source="ANALYZER", filename="x.json",
            object_key="x2", size_bytes=1, sha256="c"*64, kind=EvidenceKind.DERIVED,
            scope=EvidenceScope.CALL, level=EvidenceLevel.L1, completeness=EvidenceCompleteness.COMPLETE,
            parent_evidence_ids=[parent.id], producer_type="ANALYZER_RUN", producer_id="run-1", producer_version="1.0",
        )
        relation = db.scalar(select(EvidenceRelation).where(EvidenceRelation.child_evidence_id == child.id))
        assert relation is not None and relation.parent_evidence_id == parent.id


def test_hypothesis_persistence_is_append_only_revisioned():
    eng = _engine()
    with Session(eng) as db:
        case = _case(db, status=CaseStatus.ANALYZING.value)
        job = Job(case_id=case.id, type="AI_DIAGNOSIS", status=JobStatus.RUNNING.value); db.add(job); db.flush()
        run = DiagnosisRun(case_id=case.id, job_id=job.id, status="ANALYZING"); db.add(run); db.flush()
        d1 = DiagnosisDecision([
            HypothesisProposal("H", "candidate", "RTP", .55, HypothesisState.OPEN.value, evidence=[EvidenceRef("EVIDENCE", "e1", "L1")])
        ], [], "WAITING_USER", {})
        h = persist_decision(db, run, d1)[0]
        first_revision = h.current_revision_id
        d2 = DiagnosisDecision([
            HypothesisProposal("H", "candidate", "RTP", .88, HypothesisState.SUPPORTED.value, evidence=[EvidenceRef("EVIDENCE", "e2", "L1")])
        ], [], "DIAGNOSED", {})
        persist_decision(db, run, d2)
        db.flush()
        revisions = list(db.scalars(select(HypothesisRevision).where(HypothesisRevision.hypothesis_id == h.id).order_by(HypothesisRevision.revision_no)))
        assert len(revisions) == 2
        assert revisions[0].id == first_revision
        assert revisions[1].supersedes_revision_id == first_revision
        assert revisions[0].status == HypothesisState.OPEN.value
        assert revisions[1].status == HypothesisState.SUPPORTED.value


def test_rule_dsl_v2_boolean_expression_and_category_order():
    r_support = compile_rule({
        "key": "SUPPORT", "version": "1", "dsl_version": 2, "category": "SUPPORT", "priority": 10,
        "when": {"and": [
            {"path": "symptoms.AUDIO_STUTTER", "op": "eq", "value": True},
            {"not": {"path": "anomaly_counts.PACKET_LOSS", "op": "eq", "value": 0}},
        ]},
        "then": [{"action": "known", "payload": {"text": "support"}}],
    })
    r_safety = compile_rule({
        "key": "SAFETY", "version": "1", "dsl_version": 2, "category": "SAFETY", "priority": 999,
        "when": {"path": "case.has_evidence", "op": "exists", "value": True},
        "then": [{"action": "known", "payload": {"text": "safety"}}],
    })
    snap = {
        "case": {"summary": "通话卡顿"}, "devices": [], "evidences": [],
        "analyzers": {"packet_intelligence": {"run_id": "r", "status": "SUCCESS", "result": {"anomalies": [{"type":"PACKET_LOSS"}], "calls": [], "registrations": [], "rtp_streams": []}}},
    }
    _, matches, _ = RuleEngine().evaluate(snap, [r_support, r_safety])
    assert [x.rule_key for x in matches] == ["SAFETY", "SUPPORT"]
    assert all(x.matched for x in matches)
    assert r_safety.category == RuleCategory.SAFETY


def test_rbac_dependency_denies_role_outside_allowlist():
    dep = require_roles(UserRole.EXPERT_REVIEWER, UserRole.ADMIN)
    with pytest.raises(AppError) as exc:
        dep(identity=AuthIdentity("eng", UserRole.ENGINEER, True))
    assert exc.value.code == "PERMISSION_DENIED"
    allowed = dep(identity=AuthIdentity("reviewer", UserRole.EXPERT_REVIEWER, True))
    assert allowed.actor_id == "reviewer"
