import json

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db import models as _models  # noqa: F401
from app.db import conversation_models as _conversation_models  # noqa: F401
from app.db.models import Case, Evidence
from app.integrations.feishu import events
from app.integrations.feishu.case_resolver import active_case_for_chat
from app.integrations.feishu.service import bind_case_to_chat


def _engine():
    eng = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(eng)
    return eng


def _ctx(message_id: str) -> dict:
    return {
        "tenant_key": "tenant-a",
        "chat_id": "oc-boundary-ack",
        "message_id": message_id,
        "root_message_id": None,
        "parent_message_id": None,
        "sender_open_id": "ou-engineer",
        "chat_type": "group",
        "normalized_text": "旧故障",
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
            "chat_id": "oc-boundary-ack",
            "chat_type": "group",
            "sender": {"sender_id": {"open_id": "ou-engineer"}},
            "message": {
                "chat_id": "oc-boundary-ack",
                "chat_type": "group",
                "message_id": message_id,
                "message_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
            },
        },
    }


def test_new_case_ack_is_not_hidden_by_missing_device_prompt(monkeypatch):
    replies: list[tuple[str, str]] = []
    monkeypatch.setattr(events, "enqueue_reply", lambda message_id, text: replies.append((message_id, text)))

    with Session(_engine()) as db:
        old_case = Case(
            case_no="VOIP-OLD-BOUNDARY",
            summary="旧故障",
            status="WAITING_USER",
        )
        db.add(old_case)
        db.flush()
        old_case_id = old_case.id
        bind_case_to_chat(
            db,
            case_id=old_case.id,
            chat_id="oc-boundary-ack",
            chat_type="group",
            source_context=_ctx("msg-old"),
        )
        db.commit()

        text = "这是新的故障，不要关联上一个 Case。另一台设备出现单通无声，请新建一个 Case 开始分析。"
        result = events.dispatch_event(
            db,
            payload=_payload("msg-new", text),
            actor="feishu:ou-engineer",
        )
        db.commit()

        assert result["handled"] == "needs_clarification"
        assert result["correlation_reason"] == "CASE_BOUNDARY_NEW_CASE"
        assert result["case_id"] != old_case_id
        assert result["case_no"]
        assert result["previous_case_no"] == "VOIP-OLD-BOUNDARY"

        old_case_after = db.get(Case, old_case_id)
        assert old_case_after is not None
        assert old_case_after.status == "WAITING_USER"
        assert db.scalar(select(func.count()).select_from(Evidence)) == 0

        active_case, _binding_id = active_case_for_chat(
            db,
            tenant_key="tenant-a",
            chat_id="oc-boundary-ack",
        )
        assert active_case is not None
        assert active_case.id == result["case_id"]

        assert len(replies) == 1
        reply = replies[0][1]
        assert f"已创建并切换到新 Case {result['case_no']}" in reply
        assert "旧 Case VOIP-OLD-BOUNDARY 的历史证据仍保留且状态未改" in reply
        assert "本条故障描述已归入新 Case" in reply
        assert "设备 URL" in reply
        assert "IP+SN" in reply
        assert "PCAP/PCAPNG" in reply
