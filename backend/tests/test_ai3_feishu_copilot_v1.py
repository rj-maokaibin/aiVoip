from __future__ import annotations

import json

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.contracts.enums import UserRole
from app.copilot.service import CopilotResult
from app.core.config import settings
from app.db.ai_intelligence_models import AICaseCopilotRecord
from app.db.base import Base
from app.db.feishu_governance_models import FeishuUserIdentity
from app.db.models import Case, CaseDevice, ReproductionSession
from app.integrations.feishu import authorized_events
from app.integrations.feishu.authorized_events import dispatch_authorized_event
from app.integrations.feishu.service import bind_case_to_chat


def _engine():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return engine


def _identity(db: Session, *, open_id: str, role: str = "VIEWER", status: str = "ACTIVE"):
    row = FeishuUserIdentity(
        tenant_key="tenant-ai3",
        open_id=open_id,
        internal_actor_id=f"actor:{open_id}",
        role=role,
        status=status,
    )
    db.add(row)
    db.flush()
    return row


def _payload(text: str, *, open_id: str, message_id: str) -> dict:
    return {
        "header": {
            "event_type": "im.message.receive_v1",
            "event_id": f"evt-{message_id}",
            "tenant_key": "tenant-ai3",
        },
        "operator": {"open_id": open_id},
        "event": {
            "chat_id": "oc-ai3",
            "chat_type": "group",
            "message": {
                "message_id": message_id,
                "chat_id": "oc-ai3",
                "chat_type": "group",
                "message_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
            },
        },
    }


def _case(db: Session):
    case = Case(case_no="CASE-AI3-FEISHU", summary="周期性电流音", status="ANALYZING")
    db.add(case)
    db.flush()
    bind_case_to_chat(
        db,
        case_id=case.id,
        chat_id="oc-ai3",
        chat_type="group",
        source_context={
            "tenant_key": "tenant-ai3",
            "message_id": "msg-root-ai3",
            "sender_open_id": "ou-owner",
        },
    )
    db.flush()
    return case


def _case_with_session(db: Session):
    case = _case(db)
    device = CaseDevice(
        case_id=case.id,
        ip="10.0.0.8",
        ssh_port=22,
        sn="SN-AI3",
        username="root",
    )
    db.add(device)
    db.flush()
    session = ReproductionSession(
        case_id=case.id,
        device_id=device.id,
        profile_key="VOIP_GENERIC_FULL_CAPTURE",
        profile_version="1.0.0",
        profile_checksum="a" * 64,
        effective_profile_snapshot={},
    )
    db.add(session)
    db.flush()
    return case, session


class _FakeCopilotService:
    calls = 0

    def answer(self, db, **kwargs):
        self.__class__.calls += 1
        return CopilotResult(
            status="ANSWERED",
            answer="当前 Case 证据显示 RTP 异常，但根因尚未确认。",
            proposal={"schema_version": "ai-case-copilot-v1"},
            grounding={"status": "PASS"},
            record_id="copilot-feishu-1",
        )


class _ForbiddenCopilotService:
    def __init__(self, *args, **kwargs):
        raise AssertionError("Copilot must not be reached")


class _FailingCopilotService:
    calls = 0

    def answer(self, db, **kwargs):
        self.__class__.calls += 1
        # Prove the SAVEPOINT rolls back partial AI3 persistence while preserving
        # the outer Feishu identity/Case/idempotency transaction.
        db.add(AICaseCopilotRecord(
            case_id=kwargs["case_id"],
            request_key="transient-runtime-row",
            actor_id=kwargs["actor_id"],
            actor_role=kwargs["actor_role"].value,
            question_hash="a" * 64,
            snapshot_fingerprint="b" * 64,
            status="ANSWERED",
            proposal_json={},
            grounding_report_json={},
            prompt_version="ai-case-copilot-v1",
        ))
        db.flush()
        raise RuntimeError("synthetic AI3 runtime failure")


def test_authorized_case_general_question_uses_copilot_and_duplicate_replies_once(monkeypatch):
    monkeypatch.setattr(settings, "feishu_identity_rbac_enabled", True)
    monkeypatch.setattr(settings, "ai_case_copilot_enabled", True)
    monkeypatch.setattr(settings, "ai_semantic_router_enabled", False)
    replies = []
    _FakeCopilotService.calls = 0
    monkeypatch.setattr("app.copilot.service.CaseCopilotService", _FakeCopilotService)
    monkeypatch.setattr(authorized_events, "enqueue_reply", lambda message_id, text: replies.append((message_id, text)))

    with Session(_engine()) as db:
        case = _case(db)
        _identity(db, open_id="ou-viewer")
        payload = _payload("目前这个 Case 的证据说明了什么？", open_id="ou-viewer", message_id="msg-ai3-q1")
        first = dispatch_authorized_event(db, payload=payload)
        db.commit()
        second = dispatch_authorized_event(db, payload=payload)
        db.commit()
        assert first["handled"] == "case_copilot"
        assert first["case_id"] == case.id
        assert first["read_only"] is True
        assert second["duplicate"] is True
        assert _FakeCopilotService.calls == 1
        assert len(replies) == 1
        assert "根因尚未确认" in replies[0][1]


def test_unexpected_copilot_failure_rolls_back_sidecar_only_and_replays_safe_result(monkeypatch):
    monkeypatch.setattr(settings, "feishu_identity_rbac_enabled", True)
    monkeypatch.setattr(settings, "ai_case_copilot_enabled", True)
    monkeypatch.setattr(settings, "ai_semantic_router_enabled", False)
    replies = []
    _FailingCopilotService.calls = 0
    monkeypatch.setattr("app.copilot.service.CaseCopilotService", _FailingCopilotService)
    monkeypatch.setattr(authorized_events, "enqueue_reply", lambda message_id, text: replies.append((message_id, text)))

    with Session(_engine()) as db:
        case = _case(db)
        identity = _identity(db, open_id="ou-runtime-viewer")
        payload = _payload("这个 Case 现在怎么看？", open_id="ou-runtime-viewer", message_id="msg-ai3-runtime")
        first = dispatch_authorized_event(db, payload=payload)
        db.commit()
        second = dispatch_authorized_event(db, payload=payload)
        db.commit()

        assert first["handled"] == "case_copilot"
        assert first["copilot_status"] == "RUNTIME_FAILED"
        assert first["error_code"] == "RuntimeError"
        assert second["duplicate"] is True
        assert _FailingCopilotService.calls == 1
        assert len(replies) == 1
        assert "确定性诊断" in replies[0][1]
        assert db.get(Case, case.id) is not None
        assert db.get(FeishuUserIdentity, identity.id) is not None
        assert db.scalar(select(AICaseCopilotRecord).where(
            AICaseCopilotRecord.request_key == "transient-runtime-row"
        )) is None


def test_unknown_identity_is_denied_before_copilot(monkeypatch):
    monkeypatch.setattr(settings, "feishu_identity_rbac_enabled", True)
    monkeypatch.setattr(settings, "ai_case_copilot_enabled", True)
    monkeypatch.setattr(settings, "feishu_identity_discover_unmapped", True)
    monkeypatch.setattr("app.copilot.service.CaseCopilotService", _ForbiddenCopilotService)
    monkeypatch.setattr(authorized_events, "enqueue_reply", lambda *args, **kwargs: None)
    with Session(_engine()) as db:
        _case(db)
        result = dispatch_authorized_event(
            db,
            payload=_payload("目前这个 Case 的证据说明了什么？", open_id="ou-unknown", message_id="msg-ai3-unknown"),
        )
        assert result["handled"] == "permission_denied"
        assert result["identity_status"] == "PENDING_MAPPING"


def test_control_message_stays_on_deterministic_control_path_not_copilot(monkeypatch):
    monkeypatch.setattr(settings, "feishu_identity_rbac_enabled", True)
    monkeypatch.setattr(settings, "ai_case_copilot_enabled", True)
    monkeypatch.setattr(settings, "ai_semantic_router_enabled", False)
    monkeypatch.setattr("app.copilot.service.CaseCopilotService", _ForbiddenCopilotService)
    calls = {"cancel": 0}
    monkeypatch.setattr(
        "app.integrations.feishu.events.cancel_reproduction.apply_async",
        lambda *args, **kwargs: calls.__setitem__("cancel", calls["cancel"] + 1),
        raising=False,
    )
    monkeypatch.setattr("app.integrations.feishu.events.enqueue_reply", lambda *args, **kwargs: None)
    with Session(_engine()) as db:
        case, _session = _case_with_session(db)
        _identity(db, open_id="ou-engineer", role=UserRole.ENGINEER.value)
        result = dispatch_authorized_event(
            db,
            payload=_payload(f"停止复现 {case.case_no}", open_id="ou-engineer", message_id="msg-ai3-stop"),
        )
        assert result["handled"] == "stop_reproduction"
        assert calls["cancel"] == 1
