from __future__ import annotations

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.db.ai_intelligence_models import AIDiagnosticCycle
from app.db.base import Base
from app.db.models import AuditLog, Case, DiagnosisRun
from app.diagnosis.ai_cycle import CycleExecution
from app.services import diagnosis as diagnosis_service


class FakeDecision:
    def __init__(self):
        self.hypotheses = []
        self.known = ["deterministic known"]
        self.unknown = ["deterministic unknown"]
        self.excluded = []
        self.plan = []
        self.summary = {"headline": "deterministic result"}
        self.conclusion_state = "NEED_MORE_EVIDENCE"

    def to_dict(self):
        return {
            "hypotheses": [],
            "known": list(self.known),
            "unknown": list(self.unknown),
            "excluded": list(self.excluded),
            "plan": [],
            "summary": dict(self.summary),
            "conclusion_state": self.conclusion_state,
        }


def _db():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return Session(engine)


def _case_and_run(db: Session):
    case = Case(case_no="CASE-AI2-SIDECAR", summary="周期性电流音", status="ANALYZING")
    db.add(case)
    db.flush()
    run = DiagnosisRun(
        case_id=case.id,
        status="ANALYZING",
        cycle=1,
        reasoner_name="deterministic",
        reasoner_version="test",
        workflow_version="test",
    )
    db.add(run)
    db.commit()
    return case, run


def _cycle(case_id: str) -> AIDiagnosticCycle:
    return AIDiagnosticCycle(
        case_id=case_id,
        cycle_no=1,
        runtime_stage="SHADOW",
        snapshot_fingerprint="a" * 64,
        evidence_fingerprint="e" * 64,
        proposal_id=None,
        status="COMPLETED",
        known_json=[],
        unknown_json=[],
        excluded_json=[],
        hypotheses_json=[],
        critic_json={"status": "PASS"},
        next_action_json={},
        selection_json={},
        continue_recommendation="CONTINUE",
        no_progress_count=0,
        formal_result_changed=False,
        dispatch_attempted=False,
        dispatch_allowed=False,
    )


def test_persist_decision_runs_ai2_sidecar_without_mutating_formal_decision(monkeypatch):
    monkeypatch.setattr(settings, "ai_diagnostic_loop_enabled", True)
    monkeypatch.setattr(settings, "ai_promotion_stage", "SHADOW")
    seen = {}

    class _Service:
        def run_next(self, db_arg, *, case_id, actor, deterministic_baseline):
            seen.update({
                "case_id": case_id,
                "actor": actor,
                "baseline": deterministic_baseline,
            })
            row = _cycle(case_id)
            db_arg.add(row)
            db_arg.flush()
            return CycleExecution(row)

    monkeypatch.setattr(diagnosis_service, "AIDiagnosticCycleService", _Service)

    with _db() as db:
        case, run = _case_and_run(db)
        decision = FakeDecision()
        formal_before = decision.to_dict()
        diagnosis_service.persist_decision(db, run, decision)
        assert seen["case_id"] == case.id
        assert seen["actor"] == "diagnosis-worker"
        assert seen["baseline"]["diagnosis_run_id"] == run.id
        assert decision.to_dict() == formal_before
        assert not any(str(key).startswith("ai2_") for key in decision.summary)
        cycle = db.scalar(select(AIDiagnosticCycle).where(AIDiagnosticCycle.case_id == case.id))
        assert cycle is not None
        assert cycle.formal_result_changed is False
        assert cycle.dispatch_attempted is False
        assert cycle.dispatch_allowed is False


def test_ai2_sidecar_failure_rolls_back_only_sidecar_and_keeps_deterministic_flow(monkeypatch):
    monkeypatch.setattr(settings, "ai_diagnostic_loop_enabled", True)
    monkeypatch.setattr(settings, "ai_promotion_stage", "SUGGEST")

    class _FailingService:
        def run_next(self, db_arg, *, case_id, actor, deterministic_baseline):
            row = _cycle(case_id)
            row.runtime_stage = "SUGGEST"
            db_arg.add(row)
            db_arg.flush()
            raise RuntimeError("synthetic AI2 sidecar failure")

    monkeypatch.setattr(diagnosis_service, "AIDiagnosticCycleService", _FailingService)

    with _db() as db:
        case, run = _case_and_run(db)
        decision = FakeDecision()
        formal_before = decision.to_dict()
        rows = diagnosis_service.persist_decision(db, run, decision)
        assert rows == []
        assert decision.to_dict() == formal_before
        assert db.scalar(select(AIDiagnosticCycle).where(AIDiagnosticCycle.case_id == case.id)) is None
        failure = db.scalar(select(AuditLog).where(
            AuditLog.case_id == case.id,
            AuditLog.event_type == "AI_DIAGNOSTIC_CYCLE_FAILED",
        ))
        assert failure is not None
        assert failure.detail["deterministic_transaction_preserved"] is True
        assert failure.detail["dispatch_attempted"] is False
        assert failure.detail["formal_result_changed"] is False
