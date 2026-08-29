from __future__ import annotations

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.conversation.state_service import ConversationStateService
from app.db.base import Base
from app.db import models as _models  # noqa: F401
from app.db import conversation_models as _conversation_models  # noqa: F401
from app.db.models import Case, DiagnosisRun, Evidence
from app.db.conversation_models import ConversationTurn


def _engine():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return engine


def test_unknown_answer_adds_turn_not_evidence_and_does_not_resume_cycle(monkeypatch):
    from app.db import session as db_session
    from app.core.config import settings
    from app.workers.device_provision_task import ingest_feishu_follow_up

    engine = _engine()
    Local = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(db_session, "SessionLocal", Local)
    monkeypatch.setattr(settings, "conversation_cycle_decoupled", True)
    monkeypatch.setattr(settings, "conversation_ai_enabled", False)

    with Local() as db:
        case = Case(case_no="CASE-CYCLE-001", summary="现场通话异常", status="WAITING_USER")
        db.add(case)
        db.flush()
        run = DiagnosisRun(case_id=case.id, status="WAITING_USER", cycle=5)
        db.add(run)
        db.flush()
        ConversationStateService().mark_question_asked(
            db,
            case_id=case.id,
            text='请提供本次异常发生的大致时间；如果不知道，请回复“不知道”。',
            need="anomaly_timestamp",
        )
        case_id = case.id
        run_id = run.id
        db.commit()

    result = ingest_feishu_follow_up.run(
        case_id,
        "不知道",
        {
            "message_id": "msg-unknown-1",
            "sender_open_id": "ou-test",
            "tenant_key": "tenant-a",
        },
    )

    assert result["diagnosis_resumed"] is False
    assert result["evidence_id"] is None
    assert result["intent"] == "ANSWER_ACTIVE_QUESTION"

    with Local() as db:
        assert db.scalar(select(func.count(Evidence.id)).where(Evidence.case_id == case_id)) == 0
        assert db.scalar(select(func.count(ConversationTurn.id)).where(ConversationTurn.case_id == case_id)) == 1
        run = db.get(DiagnosisRun, run_id)
        assert run.cycle == 5
        assert run.status == "WAITING_USER"
        _conversation, state = ConversationStateService().case_state(db, case_id)
        assert state.slots_json["anomaly_timestamp"]["state"] == "UNKNOWN_BY_USER"


def test_progress_question_is_chat_only_even_when_case_is_waiting(monkeypatch):
    from app.db import session as db_session
    from app.core.config import settings
    from app.workers.device_provision_task import ingest_feishu_follow_up

    engine = _engine()
    Local = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(db_session, "SessionLocal", Local)
    monkeypatch.setattr(settings, "conversation_cycle_decoupled", True)
    monkeypatch.setattr(settings, "conversation_ai_enabled", False)

    with Local() as db:
        case = Case(case_no="CASE-CYCLE-002", summary="现场通话异常", status="WAITING_USER")
        db.add(case)
        db.flush()
        run = DiagnosisRun(case_id=case.id, status="WAITING_USER", cycle=4)
        db.add(run)
        db.flush()
        case_id = case.id
        run_id = run.id
        db.commit()

    result = ingest_feishu_follow_up.run(
        case_id,
        "什么时候可以结束分析",
        {"message_id": "msg-progress-1", "tenant_key": "tenant-a"},
    )
    assert result["diagnosis_resumed"] is False
    assert result["evidence_id"] is None
    assert result["intent"] == "CASE_COMPLETION_QUERY"

    with Local() as db:
        assert db.scalar(select(func.count(Evidence.id)).where(Evidence.case_id == case_id)) == 0
        assert db.get(DiagnosisRun, run_id).cycle == 4


def test_finish_control_is_conversation_only_and_suppresses_reask(monkeypatch):
    from app.db import session as db_session
    from app.core.config import settings
    from app.workers.device_provision_task import ingest_feishu_follow_up

    engine = _engine()
    Local = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(db_session, "SessionLocal", Local)
    monkeypatch.setattr(settings, "conversation_cycle_decoupled", True)
    monkeypatch.setattr(settings, "conversation_ai_enabled", False)

    with Local() as db:
        case = Case(case_no="CASE-CYCLE-003", summary="现场通话异常", status="WAITING_USER")
        db.add(case)
        db.flush()
        run = DiagnosisRun(
            case_id=case.id,
            status="WAITING_USER",
            cycle=6,
            summary_json={
                "headline": "候选方向，需补证",
                "known": ["已发现 1 份PCAP/PCAPNG"],
                "unknown": ["尚不能确认最终根因"],
            },
            decision_json={"plan": []},
        )
        db.add(run)
        db.flush()
        ConversationStateService().mark_question_asked(
            db,
            case_id=case.id,
            text="请提供本次异常发生的大致时间",
            need="anomaly_timestamp",
        )
        case_id = case.id
        run_id = run.id
        db.commit()

    result = ingest_feishu_follow_up.run(
        case_id,
        "结束本轮分析，按现有证据给出阶段结论。",
        {
            "message_id": "msg-finish-1",
            "sender_open_id": "ou-test",
            "tenant_key": "tenant-a",
        },
    )

    assert result["diagnosis_resumed"] is False
    assert result["evidence_id"] is None
    assert result["intent"] == "CONTROL"

    with Local() as db:
        assert db.scalar(select(func.count(Evidence.id)).where(Evidence.case_id == case_id)) == 0
        assert db.scalar(select(func.count(ConversationTurn.id)).where(ConversationTurn.case_id == case_id)) == 1
        run = db.get(DiagnosisRun, run_id)
        assert run.cycle == 6
        assert run.status == "WAITING_USER"
        _conversation, state = ConversationStateService().case_state(db, case_id)
        assert state.active_question_json is None
        assert state.slots_json["__conversation_control__"]["state"] == "FINISH_WITH_PARTIAL_CONCLUSION"
        blocked = ConversationStateService().mark_question_asked(
            db,
            case_id=case_id,
            text="请上传新的抓包",
            need="pcap",
        )
        assert blocked["should_ask"] is False
        assert blocked["reason"] == "PARTIAL_CONCLUSION_REQUESTED"
