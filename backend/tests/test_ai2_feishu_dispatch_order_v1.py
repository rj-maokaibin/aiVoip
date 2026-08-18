from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.db.ai_intelligence_models import AIDiagnosticCycle
from app.db.base import Base
from app.db.models import Case
from app.diagnosis.ai_suggest_bridge import AISuggestionExecution
from app.integrations.feishu import events


def _db():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return Session(engine)


def _cycle(case_id: str) -> AIDiagnosticCycle:
    return AIDiagnosticCycle(
        case_id=case_id,
        cycle_no=1,
        runtime_stage="SUGGEST",
        snapshot_fingerprint="s" * 64,
        evidence_fingerprint="e" * 64,
        proposal_id=None,
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
            "dispatch_allowed": False,
            "raw_command_allowed": False,
        },
        selection_json={},
        continue_recommendation="CONTINUE",
        no_progress_count=0,
        formal_result_changed=False,
        dispatch_attempted=False,
        dispatch_allowed=False,
        suggestion_state="PROPOSED",
    )


def test_reproduction_worker_enqueue_happens_only_after_database_commit(monkeypatch):
    monkeypatch.setattr(settings, "feishu_identity_rbac_enabled", True)
    order: list[str] = []

    class _Bridge:
        def accept(self, db_arg, *, case_id, cycle_id, actor, explicit_user_confirmation):
            order.append("bridge")
            row = db_arg.get(AIDiagnosticCycle, cycle_id)
            row.suggestion_state = "DISPATCHED"
            row.execution_ref_type = "reproduction_session"
            row.execution_ref_id = "session-after-commit"
            db_arg.flush()
            return AISuggestionExecution(
                cycle=row,
                kind="REPRODUCTION_PROFILE",
                registered_id="registered-repro",
                execution_ref_type="reproduction_session",
                execution_ref_id="session-after-commit",
                user_message="accepted",
                enqueue_after_commit=True,
            )

    import app.diagnosis.ai_suggest_bridge as bridge_module
    monkeypatch.setattr(bridge_module, "AISuggestionBridge", _Bridge)
    monkeypatch.setattr(
        events.start_reproduction,
        "apply_async",
        lambda *args, **kwargs: order.append("enqueue-reproduction"),
    )
    from app.workers import device_provision_task
    monkeypatch.setattr(
        device_provision_task.sync_case_card,
        "apply_async",
        lambda *args, **kwargs: order.append("sync-card"),
    )

    with _db() as db:
        case = Case(case_no="CASE-AI2-ORDER", summary="噪声", status="ANALYZING")
        db.add(case)
        db.flush()
        cycle = _cycle(case.id)
        db.add(cycle)
        db.commit()

        original_commit = db.commit

        def _commit():
            order.append("commit")
            return original_commit()

        monkeypatch.setattr(db, "commit", _commit)
        payload = {
            "header": {"event_type": "card.action.trigger"},
            "action": {
                "value": {
                    "action": "AI2_ACCEPT_SUGGESTION",
                    "case_id": case.id,
                    "cycle_id": cycle.id,
                }
            },
        }
        result = events.dispatch_event(db, payload=payload, actor="actor:engineer")
        assert result["handled"] == "ai2_suggestion_accepted"
        assert order.index("bridge") < order.index("commit")
        assert order.index("commit") < order.index("enqueue-reproduction")
        assert order.index("enqueue-reproduction") < order.index("sync-card")
