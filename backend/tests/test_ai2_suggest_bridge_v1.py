from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.db.ai_intelligence_models import AIDiagnosticCycle
from app.db.base import Base
from app.db.models import Case, DiagnosticQuestion
from app.diagnosis.ai_suggest_bridge import AISuggestionBridge, AISuggestionBridgeError


def _db():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return Session(engine)


def _case(db: Session) -> Case:
    row = Case(case_no="CASE-AI2-BRIDGE", summary="周期性电流音", status="ANALYZING")
    db.add(row)
    db.flush()
    return row


def _cycle(
    case_id: str,
    *,
    no: int = 1,
    kind: str = "QUESTION",
    registered_id: str = "AUDIO_NOISE_FAULT_LAYER",
    state: str = "PROPOSED",
) -> AIDiagnosticCycle:
    return AIDiagnosticCycle(
        case_id=case_id,
        cycle_no=no,
        runtime_stage="SUGGEST",
        snapshot_fingerprint=(str(no) * 64)[:64],
        evidence_fingerprint=(chr(96 + min(no, 26)) * 64)[:64],
        proposal_id=None,
        status="COMPLETED",
        known_json=[],
        unknown_json=["仍需判别下一层"],
        excluded_json=[],
        hypotheses_json=[],
        critic_json={"status": "PASS"},
        next_action_json={
            "type": kind,
            "registered_id": registered_id,
            "reason": "已注册判别动作",
            "dispatch_allowed": False,
            "raw_command_allowed": False,
        },
        selection_json={
            "kind": kind,
            "registered_id": registered_id,
            "dispatch_allowed": False,
            "raw_command_allowed": False,
        },
        continue_recommendation="CONTINUE",
        no_progress_count=0,
        formal_result_changed=False,
        dispatch_attempted=False,
        dispatch_allowed=False,
        suggestion_state=state,
    )


@pytest.fixture(autouse=True)
def _profile_root(monkeypatch):
    monkeypatch.setattr(settings, "profile_root", Path("profiles"))


def test_question_suggestion_requires_explicit_user_confirmation():
    with _db() as db:
        case = _case(db)
        row = _cycle(case.id)
        db.add(row)
        db.flush()
        with pytest.raises(AISuggestionBridgeError, match="AI2_EXPLICIT_USER_CONFIRMATION_REQUIRED"):
            AISuggestionBridge().accept(
                db,
                case_id=case.id,
                cycle_id=row.id,
                actor="actor:engineer",
                explicit_user_confirmation=False,
            )
        assert db.get(AIDiagnosticCycle, row.id).suggestion_state == "PROPOSED"


def test_registered_question_is_materialized_by_deterministic_registry_only():
    with _db() as db:
        case = _case(db)
        row = _cycle(case.id)
        db.add(row)
        db.flush()
        result = AISuggestionBridge().accept(
            db,
            case_id=case.id,
            cycle_id=row.id,
            actor="actor:engineer",
            explicit_user_confirmation=True,
        )
        assert result.kind == "QUESTION"
        assert result.registered_id == "AUDIO_NOISE_FAULT_LAYER"
        assert result.execution_ref_type == "diagnostic_question"
        assert result.execution_ref_id
        stored = db.get(AIDiagnosticCycle, row.id)
        assert stored.suggestion_state == "DISPATCHED"
        assert stored.accepted_by == "actor:engineer"
        assert stored.dispatch_attempted is False
        assert stored.dispatch_allowed is False
        assert stored.formal_result_changed is False
        question = db.get(DiagnosticQuestion, result.execution_ref_id)
        assert question is not None
        assert question.question_key == "AUDIO_NOISE_FAULT_LAYER"


def test_duplicate_click_is_idempotent_and_does_not_create_second_question():
    with _db() as db:
        case = _case(db)
        row = _cycle(case.id)
        db.add(row)
        db.flush()
        bridge = AISuggestionBridge()
        first = bridge.accept(
            db,
            case_id=case.id,
            cycle_id=row.id,
            actor="actor:engineer",
            explicit_user_confirmation=True,
        )
        second = bridge.accept(
            db,
            case_id=case.id,
            cycle_id=row.id,
            actor="actor:engineer",
            explicit_user_confirmation=True,
        )
        assert second.idempotent_replay is True
        assert second.execution_ref_id == first.execution_ref_id
        assert db.scalar(select(func.count()).select_from(DiagnosticQuestion).where(DiagnosticQuestion.case_id == case.id)) == 1


def test_stale_suggestion_is_rejected_even_if_registered():
    with _db() as db:
        case = _case(db)
        old = _cycle(case.id, no=1)
        latest = _cycle(case.id, no=2)
        db.add_all([old, latest])
        db.flush()
        with pytest.raises(AISuggestionBridgeError, match="AI2_SUGGESTION_STALE"):
            AISuggestionBridge().accept(
                db,
                case_id=case.id,
                cycle_id=old.id,
                actor="actor:engineer",
                explicit_user_confirmation=True,
            )
        assert old.suggestion_state == "PROPOSED"
        assert latest.suggestion_state == "PROPOSED"


def test_unregistered_question_never_enters_deterministic_workflow():
    with _db() as db:
        case = _case(db)
        row = _cycle(case.id, registered_id="MODEL_INVENTED_QUESTION")
        db.add(row)
        db.flush()
        with pytest.raises(Exception):
            AISuggestionBridge().accept(
                db,
                case_id=case.id,
                cycle_id=row.id,
                actor="actor:engineer",
                explicit_user_confirmation=True,
            )
        assert row.suggestion_state == "FAILED"
        assert row.execution_ref_id is None
        assert db.scalar(select(func.count()).select_from(DiagnosticQuestion).where(DiagnosticQuestion.case_id == case.id)) == 0


def test_raw_command_marker_is_rejected_before_registry_or_orchestrator():
    with _db() as db:
        case = _case(db)
        row = _cycle(case.id)
        row.next_action_json = {
            **row.next_action_json,
            "raw_command_allowed": True,
            "command": "ssh root@device reboot",
        }
        db.add(row)
        db.flush()
        with pytest.raises(AISuggestionBridgeError, match="AI2_RAW_COMMAND_FORBIDDEN"):
            AISuggestionBridge().accept(
                db,
                case_id=case.id,
                cycle_id=row.id,
                actor="actor:engineer",
                explicit_user_confirmation=True,
            )
        assert row.suggestion_state == "PROPOSED"
