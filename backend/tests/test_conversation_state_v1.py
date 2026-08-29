from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db import models as _models  # noqa: F401
from app.db import conversation_models as _conversation_models  # noqa: F401
from app.conversation.state_service import ConversationStateService
from app.db.models import Case


def _db():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return Session(engine)


def test_unknown_timestamp_closes_question_and_blocks_reask():
    with _db() as db:
        case = Case(case_no="CASE-CONV-001", summary="用户反馈通话异常", status="WAITING_USER")
        db.add(case)
        db.commit()
        service = ConversationStateService()
        first = service.mark_question_asked(
            db,
            case_id=case.id,
            text='请提供本次异常发生的大致时间；如果不知道，请回复“不知道”。',
            need="anomaly_timestamp",
        )
        assert first["should_ask"] is True
        conversation, state = service.case_state(db, case.id)
        assert conversation is not None
        assert state.active_question_json["slot_key"] == "anomaly_timestamp"

        service.record_user_turn(
            db,
            case_id=case.id,
            source_context={"message_id": "m-unknown"},
            text="不知道",
            interpretation={
                "intent": "ANSWER_ACTIVE_QUESTION",
                "classification": "CHAT_ONLY",
                "route_mode": "CASE_CHAT",
                "material_diagnostic_context": False,
                "active_question_answer": {
                    "slot_key": "anomaly_timestamp",
                    "state": "UNKNOWN_BY_USER",
                    "value": None,
                    "confidence": 0.99,
                },
                "entities": {},
            },
        )
        db.flush()
        _conversation, state = service.case_state(db, case.id)
        assert state.active_question_json is None
        assert state.slots_json["anomaly_timestamp"]["state"] == "UNKNOWN_BY_USER"
        assert "anomaly_timestamp" in state.unavailable_needs_json

        second = service.mark_question_asked(
            db,
            case_id=case.id,
            text='请提供本次异常发生的大致时间；如果不知道，请回复“不知道”。',
            need="anomaly_timestamp",
        )
        assert second["should_ask"] is False
        assert second["reason"] == "SLOT_ALREADY_UNAVAILABLE"


def test_material_timestamp_answer_updates_context_hash():
    with _db() as db:
        case = Case(case_no="CASE-CONV-002", summary="用户反馈通话异常", status="WAITING_USER")
        db.add(case)
        db.commit()
        service = ConversationStateService()
        service.mark_question_asked(
            db,
            case_id=case.id,
            text="请提供本次异常发生的大致时间",
            need="anomaly_timestamp",
        )
        turn = service.record_user_turn(
            db,
            case_id=case.id,
            source_context={"message_id": "m-time"},
            text="0817",
            interpretation={
                "intent": "ANSWER_ACTIVE_QUESTION",
                "classification": "DIAGNOSTIC_CONTEXT",
                "route_mode": "DIAGNOSIS_FOLLOW_UP",
                "material_diagnostic_context": True,
                "active_question_answer": {
                    "slot_key": "anomaly_timestamp",
                    "state": "ANSWERED",
                    "value": "08:17",
                    "confidence": 0.98,
                },
                "entities": {"anomaly_timestamp": "08:17"},
            },
        )
        assert turn.material_diagnostic_context is True
        _conversation, state = service.case_state(db, case.id)
        assert state.material_context_hash and len(state.material_context_hash) == 64
        assert state.slots_json["anomaly_timestamp"]["value"] == "08:17"


def test_finish_control_persists_and_blocks_new_questions_until_continue():
    with _db() as db:
        case = Case(case_no="CASE-CONV-003", summary="用户反馈通话异常", status="WAITING_USER")
        db.add(case)
        db.commit()
        service = ConversationStateService()
        first = service.mark_question_asked(
            db,
            case_id=case.id,
            text="请提供本次异常发生的大致时间",
            need="anomaly_timestamp",
        )
        assert first["should_ask"] is True

        finish_turn = service.record_user_turn(
            db,
            case_id=case.id,
            source_context={"message_id": "m-finish"},
            text="结束本轮分析，按现有证据给出阶段结论。",
            interpretation={
                "intent": "CONTROL",
                "classification": "CONTROL",
                "route_mode": "CONTROL",
                "material_diagnostic_context": False,
                "active_question_answer": None,
                "entities": {"control": "FINISH_WITH_PARTIAL_CONCLUSION"},
            },
        )
        assert finish_turn.material_diagnostic_context is False
        _conversation, state = service.case_state(db, case.id)
        assert state.active_question_json is None
        assert state.slots_json["__conversation_control__"]["state"] == "FINISH_WITH_PARTIAL_CONCLUSION"

        blocked = service.mark_question_asked(
            db,
            case_id=case.id,
            text="请上传新的 PCAP",
            need="pcap",
        )
        assert blocked["should_ask"] is False
        assert blocked["reason"] == "PARTIAL_CONCLUSION_REQUESTED"

        service.record_user_turn(
            db,
            case_id=case.id,
            source_context={"message_id": "m-continue"},
            text="继续分析",
            interpretation={
                "intent": "CONTROL",
                "classification": "CONTROL",
                "route_mode": "CONTROL",
                "material_diagnostic_context": False,
                "active_question_answer": None,
                "entities": {"control": "CONTINUE_ANALYSIS"},
            },
        )
        _conversation, state = service.case_state(db, case.id)
        assert "__conversation_control__" not in state.slots_json
        resumed = service.mark_question_asked(
            db,
            case_id=case.id,
            text="请上传新的 PCAP",
            need="pcap",
        )
        assert resumed["should_ask"] is True
