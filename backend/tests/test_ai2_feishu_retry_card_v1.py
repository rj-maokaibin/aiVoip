from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.ai_intelligence_models import AIDiagnosticCycle
from app.db.base import Base
from app.db.models import Case
from app.integrations.feishu.cards import FeishuCaseCardBuilder


def _db():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return Session(engine)


def _cycle(case_id: str, *, state: str) -> AIDiagnosticCycle:
    return AIDiagnosticCycle(
        case_id=case_id,
        cycle_no=1,
        runtime_stage="SUGGEST",
        snapshot_fingerprint="s" * 64,
        evidence_fingerprint="e" * 64,
        status="COMPLETED",
        known_json=[],
        unknown_json=[],
        excluded_json=[],
        hypotheses_json=[],
        critic_json={"status": "PASS"},
        next_action_json={
            "type": "REPRODUCTION_PROFILE",
            "registered_id": "audio_noise_deep_capture",
            "reason": "registered reproduction discriminator",
            "raw_command_allowed": False,
        },
        selection_json={},
        continue_recommendation="CONTINUE",
        no_progress_count=0,
        formal_result_changed=False,
        dispatch_attempted=False,
        dispatch_allowed=False,
        suggestion_state=state,
        execution_ref_type="reproduction_session" if state in {"ACCEPTED", "DISPATCHED"} else None,
        execution_ref_id="session-existing" if state in {"ACCEPTED", "DISPATCHED"} else None,
    )


def test_accepted_reproduction_publish_failure_keeps_retry_button_visible():
    with _db() as db:
        case = Case(case_no="CASE-AI2-RETRY", summary="noise", status="ANALYZING")
        db.add(case)
        db.flush()
        db.add(_cycle(case.id, state="ACCEPTED"))
        db.flush()
        text = str(FeishuCaseCardBuilder().build(db, case.id).card)
        assert "复现 Session 已创建，等待/重试任务投递" in text
        assert "重试 AI2 任务投递" in text
        assert "session-existing" not in text


def test_dispatched_reproduction_hides_accept_and_retry_buttons():
    with _db() as db:
        case = Case(case_no="CASE-AI2-DISPATCHED", summary="noise", status="ANALYZING")
        db.add(case)
        db.flush()
        db.add(_cycle(case.id, state="DISPATCHED"))
        db.flush()
        text = str(FeishuCaseCardBuilder().build(db, case.id).card)
        assert "已采纳并进入确定性工作流" in text
        assert "采纳 AI2 建议" not in text
        assert "重试 AI2 任务投递" not in text
