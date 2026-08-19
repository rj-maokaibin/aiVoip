from __future__ import annotations

import json

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.contracts.enums import UserRole
from app.copilot.service import CopilotResult
from app.core.config import settings
from app.db.base import Base
from app.db.feishu_governance_models import FeishuUserIdentity
from app.db.models import Case
from app.integrations.feishu import authorized_events
from app.integrations.feishu.authorized_events import (
    _case_copilot_idempotency_key,
    dispatch_authorized_event,
)
from app.integrations.feishu.service import bind_case_to_chat


def _engine():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return engine


def _identity(db: Session, *, tenant: str, open_id: str, actor: str):
    row = FeishuUserIdentity(
        tenant_key=tenant,
        open_id=open_id,
        internal_actor_id=actor,
        role=UserRole.VIEWER.value,
        status="ACTIVE",
    )
    db.add(row)
    db.flush()
    return row


def _case(db: Session, *, tenant: str, chat_id: str, case_no: str):
    case = Case(case_no=case_no, summary="周期性电流音", status="ANALYZING")
    db.add(case)
    db.flush()
    bind_case_to_chat(
        db,
        case_id=case.id,
        chat_id=chat_id,
        chat_type="group",
        source_context={
            "tenant_key": tenant,
            "message_id": f"root-{case_no}",
            "sender_open_id": "ou-owner",
        },
    )
    db.flush()
    return case


def _payload(*, tenant: str, chat_id: str, open_id: str, message_id: str):
    return {
        "header": {
            "event_type": "im.message.receive_v1",
            "event_id": f"evt-{message_id}",
            "tenant_key": tenant,
        },
        "operator": {"open_id": open_id},
        "event": {
            "chat_id": chat_id,
            "chat_type": "group",
            "message": {
                "message_id": message_id,
                "chat_id": chat_id,
                "chat_type": "group",
                "message_type": "text",
                "content": json.dumps({"text": "目前这个 Case 的证据说明了什么？"}, ensure_ascii=False),
            },
        },
    }


class _RecordingCopilotService:
    calls: list[dict] = []

    def answer(self, db, **kwargs):
        self.__class__.calls.append(dict(kwargs))
        return CopilotResult(
            status="ANSWERED",
            answer=f"case={kwargs['case_id']}，根因尚未确认。",
            proposal={"schema_version": "ai-case-copilot-v1"},
            grounding={"status": "PASS"},
            record_id=f"copilot-{len(self.__class__.calls)}",
        )


def test_feishu_copilot_idempotency_key_is_bounded_and_context_scoped():
    base = dict(case_id="case-a", actor_id="actor-a", actor_role="VIEWER", delivery_id="msg-same")
    a = _case_copilot_idempotency_key(tenant_key="tenant-a", **base)
    b = _case_copilot_idempotency_key(tenant_key="tenant-b", **base)
    c = _case_copilot_idempotency_key(tenant_key="tenant-a", **{**base, "actor_id": "actor-b"})
    d = _case_copilot_idempotency_key(tenant_key="tenant-a", **{**base, "actor_role": "ENGINEER"})
    assert len(a) == 64
    assert len({a, b, c, d}) == 4
    assert "tenant" not in a
    assert "actor" not in a


def test_same_feishu_message_id_in_two_tenants_does_not_cross_replay(monkeypatch):
    monkeypatch.setattr(settings, "feishu_identity_rbac_enabled", True)
    monkeypatch.setattr(settings, "ai_case_copilot_enabled", True)
    monkeypatch.setattr(settings, "ai_semantic_router_enabled", False)
    _RecordingCopilotService.calls = []
    replies = []
    monkeypatch.setattr("app.copilot.service.CaseCopilotService", _RecordingCopilotService)
    monkeypatch.setattr(authorized_events, "enqueue_reply", lambda message_id, text: replies.append((message_id, text)))

    with Session(_engine()) as db:
        case_a = _case(db, tenant="tenant-a", chat_id="chat-a", case_no="CASE-AI3-TENANT-A")
        case_b = _case(db, tenant="tenant-b", chat_id="chat-b", case_no="CASE-AI3-TENANT-B")
        _identity(db, tenant="tenant-a", open_id="ou-same", actor="actor-a")
        _identity(db, tenant="tenant-b", open_id="ou-same", actor="actor-b")
        db.commit()

        payload_a = _payload(tenant="tenant-a", chat_id="chat-a", open_id="ou-same", message_id="msg-collision")
        payload_b = _payload(tenant="tenant-b", chat_id="chat-b", open_id="ou-same", message_id="msg-collision")

        first_a = dispatch_authorized_event(db, payload=payload_a)
        db.commit()
        first_b = dispatch_authorized_event(db, payload=payload_b)
        db.commit()
        replay_a = dispatch_authorized_event(db, payload=payload_a)
        db.commit()

        assert first_a["case_id"] == case_a.id
        assert first_b["case_id"] == case_b.id
        assert first_a.get("duplicate") is not True
        assert first_b.get("duplicate") is not True
        assert replay_a["duplicate"] is True
        assert len(_RecordingCopilotService.calls) == 2
        assert {_RecordingCopilotService.calls[0]["case_id"], _RecordingCopilotService.calls[1]["case_id"]} == {case_a.id, case_b.id}
        assert _RecordingCopilotService.calls[0]["request_key"] != _RecordingCopilotService.calls[1]["request_key"]
        assert len(replies) == 2
