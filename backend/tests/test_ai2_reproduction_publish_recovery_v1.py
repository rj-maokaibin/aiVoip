from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.ai_intelligence_models import AIDiagnosticCycle
from app.db.base import Base
from app.db.models import Case
from app.diagnosis.ai_suggest_bridge import AISuggestionBridge
from app.workers import ai2_dispatch_signals as signals


def _engine():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return engine


def _accepted_cycle(db: Session, *, session_id: str) -> AIDiagnosticCycle:
    case = Case(case_no=f"CASE-AI2-PUBLISH-{session_id}", summary="publish recovery", status="ANALYZING")
    db.add(case)
    db.flush()
    row = AIDiagnosticCycle(
        case_id=case.id,
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
            "type": "REPRODUCTION_PROFILE",
            "registered_id": "registered-repro",
            "reason": "test",
            "raw_command_allowed": False,
        },
        selection_json={},
        continue_recommendation="CONTINUE",
        no_progress_count=0,
        formal_result_changed=False,
        dispatch_attempted=False,
        dispatch_allowed=False,
        suggestion_state="ACCEPTED",
        accepted_by="actor:engineer",
        execution_ref_type="reproduction_session",
        execution_ref_id=session_id,
    )
    db.add(row)
    db.commit()
    return row


def test_accepted_reproduction_retry_reuses_same_session_without_new_workflow():
    engine = _engine()
    with Session(engine) as db:
        row = _accepted_cycle(db, session_id="session-retry")
        execution = AISuggestionBridge().accept(
            db,
            case_id=row.case_id,
            cycle_id=row.id,
            actor="actor:engineer",
            explicit_user_confirmation=True,
        )
        assert execution.idempotent_replay is True
        assert execution.enqueue_after_commit is True
        assert execution.execution_ref_id == "session-retry"
        assert row.suggestion_state == "ACCEPTED"


def test_after_task_publish_marks_matching_accepted_cycle_dispatched(monkeypatch):
    engine = _engine()
    SessionFactory = sessionmaker(bind=engine)
    with SessionFactory() as db:
        row = _accepted_cycle(db, session_id="session-published")
        cycle_id = row.id

    monkeypatch.setattr(signals, "SessionLocal", SessionFactory)
    changed = signals.confirm_ai2_reproduction_publish(
        sender="reproduction.start",
        headers={"task": "reproduction.start"},
        body=(["session-published"], {}, {}),
    )
    assert changed is True

    with SessionFactory() as db:
        stored = db.get(AIDiagnosticCycle, cycle_id)
        assert stored.suggestion_state == "DISPATCHED"
        assert stored.execution_ref_id == "session-published"
        assert stored.dispatch_attempted is False
        assert stored.dispatch_allowed is False
        assert stored.formal_result_changed is False


def test_unrelated_publish_does_not_modify_ai2_cycle(monkeypatch):
    engine = _engine()
    SessionFactory = sessionmaker(bind=engine)
    with SessionFactory() as db:
        row = _accepted_cycle(db, session_id="session-untouched")
        cycle_id = row.id

    monkeypatch.setattr(signals, "SessionLocal", SessionFactory)
    changed = signals.confirm_ai2_reproduction_publish(
        sender="diagnosis.run",
        headers={"task": "diagnosis.run"},
        body=(["session-untouched"], {}, {}),
    )
    assert changed is False
    with SessionFactory() as db:
        assert db.get(AIDiagnosticCycle, cycle_id).suggestion_state == "ACCEPTED"


def test_malformed_publish_body_is_ignored(monkeypatch):
    engine = _engine()
    SessionFactory = sessionmaker(bind=engine)
    monkeypatch.setattr(signals, "SessionLocal", SessionFactory)
    assert signals.confirm_ai2_reproduction_publish(
        sender="reproduction.start",
        headers={"task": "reproduction.start"},
        body=None,
    ) is False
