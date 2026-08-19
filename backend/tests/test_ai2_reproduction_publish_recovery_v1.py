from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.ai_intelligence_models import AIDiagnosticCycle
from app.db.base import Base
from app.db.models import Case, CaseDevice, ReproductionSession
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


def _accepted_cycle(
    db: Session,
    *,
    session_id: str,
    session_state: str = "CREATED",
    accepted_at: datetime | None = None,
    pending_since: datetime | None = None,
) -> AIDiagnosticCycle:
    case = Case(case_no=f"CASE-AI2-PUBLISH-{session_id}", summary="publish recovery", status="ANALYZING")
    db.add(case)
    db.flush()
    device = CaseDevice(case_id=case.id, ip="192.0.2.10", sn=f"SN-{session_id}", username="root")
    db.add(device)
    db.flush()
    session = ReproductionSession(
        id=session_id,
        case_id=case.id,
        device_id=device.id,
        profile_key="registered-repro",
        profile_version="1.0.0",
        profile_checksum="c" * 64,
        effective_profile_snapshot={"profile_key": "registered-repro"},
        state=session_state,
    )
    db.add(session)
    db.flush()
    marker = None
    if pending_since is not None:
        marker = f"AI2_REPRODUCTION_PUBLISH_PENDING:{int(pending_since.timestamp())}"
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
        accepted_at=accepted_at,
        execution_ref_type="reproduction_session",
        execution_ref_id=session_id,
        suggestion_error_code=marker,
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
        assert str(row.suggestion_error_code).startswith("AI2_REPRODUCTION_PUBLISH_PENDING:")


def test_recent_publish_lease_suppresses_concurrent_duplicate_publish():
    engine = _engine()
    now = datetime.now(timezone.utc)
    with Session(engine) as db:
        row = _accepted_cycle(
            db,
            session_id="session-lease-active",
            accepted_at=now,
            pending_since=now,
        )
        execution = AISuggestionBridge().accept(
            db,
            case_id=row.case_id,
            cycle_id=row.id,
            actor="actor:engineer",
            explicit_user_confirmation=True,
        )
        assert execution.idempotent_replay is True
        assert execution.enqueue_after_commit is False
        assert "投递处理中" in execution.user_message
        assert row.suggestion_state == "ACCEPTED"


def test_expired_publish_lease_allows_republish_of_same_session():
    engine = _engine()
    old = datetime.now(timezone.utc) - timedelta(seconds=120)
    with Session(engine) as db:
        row = _accepted_cycle(
            db,
            session_id="session-lease-expired",
            accepted_at=old,
            pending_since=old,
        )
        old_marker = row.suggestion_error_code
        execution = AISuggestionBridge().accept(
            db,
            case_id=row.case_id,
            cycle_id=row.id,
            actor="actor:engineer",
            explicit_user_confirmation=True,
        )
        assert execution.enqueue_after_commit is True
        assert execution.execution_ref_id == "session-lease-expired"
        assert row.suggestion_error_code != old_marker
        assert row.suggestion_state == "ACCEPTED"


def test_started_session_reconciles_to_dispatched_without_republish():
    engine = _engine()
    with Session(engine) as db:
        row = _accepted_cycle(
            db,
            session_id="session-already-started",
            session_state="WATCHING",
            accepted_at=datetime.now(timezone.utc),
        )
        execution = AISuggestionBridge().accept(
            db,
            case_id=row.case_id,
            cycle_id=row.id,
            actor="actor:engineer",
            explicit_user_confirmation=True,
        )
        assert execution.enqueue_after_commit is False
        assert execution.idempotent_replay is True
        assert row.suggestion_state == "DISPATCHED"
        assert row.suggestion_error_code is None


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
        assert stored.suggestion_error_code is None
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
