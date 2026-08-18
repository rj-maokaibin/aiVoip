from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.v1 import ai_cycles as api
from app.auth.providers import AuthIdentity
from app.contracts.enums import UserRole
from app.db.ai_intelligence_models import AIDiagnosticCycle
from app.db.base import Base
from app.db.models import Case
from app.diagnosis.ai_cycle import AIDiagnosticCycleError, CycleExecution


def _db():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return Session(engine)


def _identity(role=UserRole.ENGINEER):
    return AuthIdentity("actor-ai2", role, True, "test")


def _case(db):
    row = Case(case_no="CASE-AI2-API", summary="周期性电流音", status="ANALYZING")
    db.add(row)
    db.commit()
    return row


def _cycle(case_id: str, *, stage: str = "SUGGEST") -> AIDiagnosticCycle:
    return AIDiagnosticCycle(
        case_id=case_id,
        cycle_no=1,
        runtime_stage=stage,
        snapshot_fingerprint="a" * 64,
        evidence_fingerprint="e" * 64,
        proposal_id=None,
        status="COMPLETED",
        known_json=["known"],
        unknown_json=["unknown"],
        excluded_json=[],
        hypotheses_json=[],
        critic_json={"status": "PASS"},
        next_action_json={
            "type": "QUESTION",
            "registered_id": "AUDIO_NOISE_FAULT_LAYER",
            "dispatch_allowed": False,
            "raw_command_allowed": False,
        },
        selection_json={
            "kind": "QUESTION",
            "registered_id": "AUDIO_NOISE_FAULT_LAYER",
            "dispatch_allowed": False,
            "raw_command_allowed": False,
        },
        continue_recommendation="CONTINUE",
        no_progress_count=0,
        formal_result_changed=False,
        dispatch_attempted=False,
        dispatch_allowed=False,
    )


def test_list_cycles_returns_non_executing_authority_contract():
    with _db() as db:
        case = _case(db)
        row = _cycle(case.id)
        db.add(row)
        db.commit()
        result = api.list_ai_cycles(case.id, db=db, identity=_identity())
        assert result["count"] == 1
        item = result["items"][0]
        assert item["formal_result_changed"] is False
        assert item["dispatch_attempted"] is False
        assert item["dispatch_allowed"] is False
        assert item["next_action"]["registered_id"] == "AUDIO_NOISE_FAULT_LAYER"
        assert item["root_cause_authority"] == "DETERMINISTIC_OR_HUMAN_CONFIRMED_ONLY"


def test_next_cycle_api_returns_suggest_only_result_and_commits(monkeypatch):
    seen = {}

    class _Service:
        def run_next(self, db_arg, *, case_id, actor):
            seen.update({"case_id": case_id, "actor": actor})
            row = _cycle(case_id)
            db_arg.add(row)
            db_arg.flush()
            return CycleExecution(row)

    monkeypatch.setattr(api, "AIDiagnosticCycleService", _Service)
    with _db() as db:
        case = _case(db)
        result = api.run_next_ai_cycle(case.id, db=db, identity=_identity())
        assert result["runtime_stage"] == "SUGGEST"
        assert result["dispatch_attempted"] is False
        assert result["dispatch_allowed"] is False
        assert result["formal_result_changed"] is False
        assert seen == {"case_id": case.id, "actor": "actor-ai2"}
        assert db.get(AIDiagnosticCycle, result["id"]) is not None


def test_next_cycle_api_fails_closed_when_controlled_stage_is_not_allowed(monkeypatch):
    class _BlockedService:
        def run_next(self, *args, **kwargs):
            raise AIDiagnosticCycleError("AI2_CONTROLLED_PLANNER_NOT_ENABLED_BY_V1_GATE")

    monkeypatch.setattr(api, "AIDiagnosticCycleService", _BlockedService)
    with _db() as db:
        case = _case(db)
        try:
            api.run_next_ai_cycle(case.id, db=db, identity=_identity())
        except Exception as exc:
            assert getattr(exc, "status_code", None) == 409
            assert getattr(exc, "detail", None) == "AI2_CONTROLLED_PLANNER_NOT_ENABLED_BY_V1_GATE"
        else:
            raise AssertionError("CONTROLLED_PLANNER must not be opened by AI2 V1 API")
