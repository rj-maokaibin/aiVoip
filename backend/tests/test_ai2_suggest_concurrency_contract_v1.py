from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.ai_intelligence_models import AIDiagnosticCycle
from app.db.base import Base
from app.db.models import Case
from app.diagnosis.ai_suggest_bridge import AISuggestionBridge, AISuggestionBridgeError


def _cycle(case_id: str, *, state: str = "PROPOSED") -> AIDiagnosticCycle:
    return AIDiagnosticCycle(
        case_id=case_id,
        cycle_no=1,
        runtime_stage="SUGGEST",
        snapshot_fingerprint="a" * 64,
        evidence_fingerprint="b" * 64,
        status="COMPLETED",
        known_json=[],
        unknown_json=[],
        excluded_json=[],
        hypotheses_json=[],
        critic_json={"status": "PASS"},
        next_action_json={
            "type": "QUESTION",
            "registered_id": "AUDIO_NOISE_FAULT_LAYER",
            "reason": "registered discriminator",
            "raw_command_allowed": False,
        },
        selection_json={},
        continue_recommendation="CONTINUE",
        no_progress_count=0,
        formal_result_changed=False,
        dispatch_attempted=False,
        dispatch_allowed=False,
        suggestion_state=state,
    )


def test_load_current_cycle_uses_for_update_for_acceptance_serialization():
    row = _cycle("case-lock")
    row.id = "cycle-lock"

    class _RecordingSession:
        def __init__(self):
            self.lock_flags = []

        def scalar(self, stmt):
            self.lock_flags.append(getattr(stmt, "_for_update_arg", None) is not None)
            return row

    db = _RecordingSession()
    loaded = AISuggestionBridge()._load_current_cycle(db, case_id="case-lock", cycle_id="cycle-lock")
    assert loaded is row
    assert db.lock_flags == [True, True]


def _db():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return Session(engine)


@pytest.mark.parametrize("state", ["NONE", "ACCEPTED", "FAILED"])
def test_only_proposed_state_can_start_first_acceptance(state):
    with _db() as db:
        case = Case(case_no=f"CASE-AI2-{state}", summary="state contract", status="ANALYZING")
        db.add(case)
        db.flush()
        row = _cycle(case.id, state=state)
        db.add(row)
        db.flush()
        with pytest.raises(AISuggestionBridgeError, match="AI2_SUGGESTION_STATE_NOT_ACTIONABLE"):
            AISuggestionBridge().accept(
                db,
                case_id=case.id,
                cycle_id=row.id,
                actor="actor:engineer",
                explicit_user_confirmation=True,
            )
