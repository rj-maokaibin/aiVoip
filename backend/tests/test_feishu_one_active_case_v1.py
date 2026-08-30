import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.models import Case, FeishuCaseBinding
from app.integrations.feishu import events
from app.integrations.feishu.service import (
    FeishuActiveCaseConflict,
    FeishuCaseAlreadyBound,
    bind_case_to_chat,
)


def _engine():
    eng = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(eng)
    return eng


def _case(db: Session, case_no: str, status: str = "ANALYZING") -> Case:
    row = Case(case_no=case_no, summary=f"{case_no} 电流音", status=status)
    db.add(row)
    db.flush()
    return row


def _ctx(tenant: str, message: str, sender: str = "ou-engineer") -> dict:
    return {
        "tenant_key": tenant,
        "message_id": message,
        "root_message_id": None,
        "parent_message_id": None,
        "sender_open_id": sender,
        "chat_type": "group",
        "normalized_text": "电流音",
        "attachments": [],
    }


def test_bind_rejects_second_active_case_and_transaction_owner_rolls_back_loser():
    eng = _engine()
    with Session(eng) as db:
        winner = _case(db, "CASE-WINNER")
        bind_case_to_chat(
            db, case_id=winner.id, chat_id="oc-1", chat_type="group",
            source_context=_ctx("tenant-a", "m-1"),
        )
        db.commit()
        winner_id = winner.id

        loser = _case(db, "CASE-LOSER")
        loser_id = loser.id
        with pytest.raises(FeishuActiveCaseConflict) as exc:
            bind_case_to_chat(
                db, case_id=loser.id, chat_id="oc-1", chat_type="group",
                source_context=_ctx("tenant-a", "m-2"),
            )
        assert exc.value.existing_case_id == winner_id
        # Binding is a low-level service and must not roll back unrelated caller
        # state. The workflow owning Case creation rolls back the whole operation.
        assert db.get(Case, loser_id) is not None
        db.rollback()
        assert db.get(Case, loser_id) is None
        assert db.scalar(select(func.count()).select_from(FeishuCaseBinding)) == 1


def test_group_can_be_reused_after_previous_case_is_terminal():
    with Session(_engine()) as db:
        first = _case(db, "CASE-1")
        bind_case_to_chat(
            db, case_id=first.id, chat_id="oc-1", chat_type="group",
            source_context=_ctx("tenant-a", "m-1"),
        )
        first.status = "RESOLVED"
        db.commit()

        second = _case(db, "CASE-2")
        binding = bind_case_to_chat(
            db, case_id=second.id, chat_id="oc-1", chat_type="group",
            source_context=_ctx("tenant-a", "m-2"),
        )
        assert binding.case_id == second.id


def test_same_case_cannot_be_silently_rebound_to_another_chat():
    with Session(_engine()) as db:
        case = _case(db, "CASE-1")
        bind_case_to_chat(
            db, case_id=case.id, chat_id="oc-1", chat_type="group",
            source_context=_ctx("tenant-a", "m-1"),
        )
        with pytest.raises(FeishuCaseAlreadyBound):
            bind_case_to_chat(
                db, case_id=case.id, chat_id="oc-2", chat_type="group",
                source_context=_ctx("tenant-a", "m-2"),
            )


def test_same_chat_id_in_different_tenant_may_have_independent_active_case():
    with Session(_engine()) as db:
        a = _case(db, "CASE-A")
        b = _case(db, "CASE-B")
        bind_case_to_chat(db, case_id=a.id, chat_id="oc-same", chat_type="group", source_context=_ctx("tenant-a", "m-a"))
        bind_case_to_chat(db, case_id=b.id, chat_id="oc-same", chat_type="group", source_context=_ctx("tenant-b", "m-b"))
        assert db.scalar(select(func.count()).select_from(FeishuCaseBinding)) == 2


def _payload(*, tenant: str, chat: str, message_id: str, text_value: str) -> dict:
    return {
        "header": {"event_type": "im.message.receive_v1", "event_id": f"e-{message_id}", "tenant_key": tenant},
        "event": {
            "chat_id": chat,
            "chat_type": "group",
            "sender": {"sender_id": {"open_id": "ou-engineer"}},
            "message": {
                "chat_id": chat,
                "chat_type": "group",
                "message_id": message_id,
                "message_type": "text",
                "content": json.dumps({"text": text_value}, ensure_ascii=False),
            },
        },
    }


def test_active_chat_turns_explicit_continue_message_into_follow_up(monkeypatch):
    eng = _engine()
    calls = []
    replies = []
    monkeypatch.setattr(events, "enqueue_reply", lambda message_id, text: replies.append((message_id, text)))

    import app.workers.device_provision_task as worker
    monkeypatch.setattr(worker.ingest_feishu_follow_up, "apply_async", lambda *args, **kwargs: calls.append((args, kwargs)))

    with Session(eng) as db:
        case = _case(db, "CASE-A")
        bind_case_to_chat(db, case_id=case.id, chat_id="oc-1", chat_type="group", source_context=_ctx("tenant-a", "m-root"))
        db.commit()

        result = events.dispatch_event(
            db,
            payload=_payload(
                tenant="tenant-a", chat="oc-1", message_id="m-follow",
                text_value="这个设备又有电流音，帮忙继续分析",
            ),
            actor="feishu:ou-engineer",
        )

        assert result["handled"] == "case_follow_up"
        assert result["case_id"] == case.id
        assert result["correlation_reason"] == "CHAT_ACTIVE_CASE"
        assert result["intent"] == "CASE_FOLLOW_UP"
        assert calls


def test_explicit_new_fault_rotates_active_case_without_closing_previous_case(monkeypatch):
    eng = _engine()
    replies = []
    monkeypatch.setattr(events, "enqueue_reply", lambda message_id, text: replies.append((message_id, text)))

    with Session(eng) as db:
        old_case = _case(db, "CASE-A")
        old_case_id = old_case.id
        bind_case_to_chat(db, case_id=old_case.id, chat_id="oc-1", chat_type="group", source_context=_ctx("tenant-a", "m-root"))
        db.commit()

        result = events.dispatch_event(
            db,
            payload=_payload(
                tenant="tenant-a", chat="oc-1", message_id="m-new",
                text_value="这是新的故障，另外一台设备也有电流音，请帮忙分析",
            ),
            actor="feishu:ou-engineer",
        )

        assert result["handled"] == "needs_clarification"
        assert result["correlation_reason"] == "CASE_BOUNDARY_NEW_CASE"
        assert result["case_id"] != old_case_id
        assert result["missing_user_inputs"] == ["device_url_or_ip_and_sn_or_attachment"]
        assert db.get(Case, old_case_id).status == "ANALYZING"
        new_case = db.get(Case, result["case_id"])
        assert new_case is not None
        assert new_case.status == "NEW"
        assert db.scalar(select(func.count()).select_from(Case)) == 2
        assert any("设备 URL" in text or "IP+SN" in text for _, text in replies)


def test_migration_declares_lifecycle_and_partial_unique_index():
    root = Path(__file__).resolve().parents[2]
    migration = (root / "backend/migrations/versions/0021_feishu_case_governance_v1.py").read_text(encoding="utf-8")
    assert "binding_state" in migration
    assert "binding_generation" in migration
    assert "uq_feishu_active_case_per_chat" in migration
    assert "binding_state = 'ACTIVE'" in migration
    assert "source_tenant_key <> ''" in migration
