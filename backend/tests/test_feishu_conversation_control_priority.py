import json

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db import models as _models  # noqa: F401
from app.db import conversation_models as _conversation_models  # noqa: F401
from app.db.models import Case, Evidence
from app.integrations.feishu import events
from app.integrations.feishu.intake import route_intake
from app.integrations.feishu.service import bind_case_to_chat


def _engine():
    eng = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(eng)
    return eng


def _ctx(message_id: str) -> dict:
    return {
        "tenant_key": "tenant-a",
        "chat_id": "oc-control-priority",
        "message_id": message_id,
        "root_message_id": None,
        "parent_message_id": None,
        "sender_open_id": "ou-engineer",
        "chat_type": "group",
        "normalized_text": "现有故障",
        "attachments": [],
    }


def _payload(message_id: str, text: str) -> dict:
    return {
        "header": {
            "event_type": "im.message.receive_v1",
            "event_id": f"evt-{message_id}",
            "tenant_key": "tenant-a",
        },
        "event": {
            "chat_id": "oc-control-priority",
            "chat_type": "group",
            "sender": {"sender_id": {"open_id": "ou-engineer"}},
            "message": {
                "chat_id": "oc-control-priority",
                "chat_type": "group",
                "message_id": message_id,
                "message_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
            },
        },
    }


def _active_case(db: Session) -> Case:
    row = Case(
        case_no="VOIP-20260830-CONTROL",
        summary="单通无声",
        status="ANALYZING",
    )
    db.add(row)
    db.flush()
    bind_case_to_chat(
        db,
        case_id=row.id,
        chat_id="oc-control-priority",
        chat_type="group",
        source_context=_ctx("msg-bind"),
    )
    db.commit()
    return row


def test_intake_routes_finish_and_continue_as_current_case_controls():
    finish = route_intake(
        text="结束本轮分析，按现有证据给出阶段结论。",
        has_thread_case=True,
    )
    assert finish.intent == "CASE_FOLLOW_UP"
    assert finish.reason == "explicit_conversation_control"

    cont = route_intake(text="继续分析", has_thread_case=True)
    assert cont.intent == "CASE_FOLLOW_UP"
    assert cont.reason == "explicit_conversation_control"

    stop_repro = route_intake(text="停止复现", has_thread_case=True)
    assert stop_repro.intent == "STOP_REPRODUCTION"


def test_finish_control_bypasses_case_boundary_and_reaches_conversation(monkeypatch):
    dispatched: list[tuple[str, str, dict]] = []
    replies: list[tuple[str, str]] = []
    monkeypatch.setattr(
        events,
        "_dispatch_case_conversation",
        lambda *, case_id, text, source_context: dispatched.append((case_id, text, source_context)),
    )
    monkeypatch.setattr(events, "enqueue_reply", lambda message_id, text: replies.append((message_id, text)))

    with Session(_engine()) as db:
        case = _active_case(db)
        case_id = case.id
        before_case_count = db.scalar(select(func.count()).select_from(Case))

        text = "结束本轮分析，按现有证据给出阶段结论。"
        result = events.dispatch_event(
            db,
            payload=_payload("msg-finish", text),
            actor="feishu:ou-engineer",
        )
        db.commit()

        assert result["handled"] == "case_follow_up"
        assert result["case_id"] == case_id
        assert result["correlation_reason"] == "CHAT_ACTIVE_CASE"
        assert result["conversation_dispatched"] is True
        assert db.scalar(select(func.count()).select_from(Case)) == before_case_count
        assert db.scalar(select(func.count()).select_from(Evidence)) == 0
        assert len(dispatched) == 1
        assert dispatched[0][0] == case_id
        assert dispatched[0][1] == text
        assert replies == []


def test_continue_control_bypasses_case_boundary_and_keeps_same_case(monkeypatch):
    dispatched: list[tuple[str, str, dict]] = []
    replies: list[tuple[str, str]] = []
    monkeypatch.setattr(
        events,
        "_dispatch_case_conversation",
        lambda *, case_id, text, source_context: dispatched.append((case_id, text, source_context)),
    )
    monkeypatch.setattr(events, "enqueue_reply", lambda message_id, text: replies.append((message_id, text)))

    with Session(_engine()) as db:
        case = _active_case(db)
        case_id = case.id
        before_case_count = db.scalar(select(func.count()).select_from(Case))

        result = events.dispatch_event(
            db,
            payload=_payload("msg-continue", "继续分析"),
            actor="feishu:ou-engineer",
        )
        db.commit()

        assert result["handled"] == "case_follow_up"
        assert result["case_id"] == case_id
        assert result["correlation_reason"] == "CHAT_ACTIVE_CASE"
        assert result["conversation_dispatched"] is True
        assert db.scalar(select(func.count()).select_from(Case)) == before_case_count
        assert db.scalar(select(func.count()).select_from(Evidence)) == 0
        assert len(dispatched) == 1
        assert dispatched[0][0] == case_id
        assert dispatched[0][1] == "继续分析"
        assert replies == []
