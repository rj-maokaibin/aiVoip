import json
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.config import settings
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


def _identity(db: Session, *, open_id: str, role: str, status: str = "ACTIVE"):
    row = FeishuUserIdentity(
        tenant_key="tenant-a", open_id=open_id,
        internal_actor_id=f"actor:{open_id}", role=role, status=status,
    )
    db.add(row)
    db.flush()
    return row


def _message_payload(text_value: str, *, open_id: str, message_id: str = "msg-1") -> dict:
    return {
        "header": {
            "event_type": "im.message.receive_v1",
            "event_id": f"evt-{message_id}",
            "tenant_key": "tenant-a",
        },
        "operator": {"open_id": open_id},
        "event": {
            "chat_id": "oc-1",
            "chat_type": "group",
            "message": {
                "message_id": message_id,
                "chat_id": "oc-1",
                "chat_type": "group",
                "message_type": "text",
                "content": json.dumps({"text": text_value}, ensure_ascii=False),
            },
        },
    }


def _case_session(db: Session):
    case = Case(case_no="CASE-RBAC-1", summary="DTMF丢号", status="ANALYZING")
    db.add(case)
    db.flush()
    device = CaseDevice(
        case_id=case.id, ip="10.0.0.8", ssh_port=22,
        sn="SN-RBAC", username="root",
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
    bind_case_to_chat(
        db, case_id=case.id, chat_id="oc-1", chat_type="group",
        source_context={
            "tenant_key": "tenant-a",
            "message_id": "msg-root",
            "sender_open_id": "ou-owner",
        },
    )
    db.flush()
    return case, session


def test_unknown_user_is_denied_before_new_diagnosis_is_enqueued(monkeypatch):
    monkeypatch.setattr(settings, "feishu_identity_rbac_enabled", True)
    monkeypatch.setattr(settings, "feishu_identity_discover_unmapped", True)
    calls = {"provision": 0}
    replies = []
    monkeypatch.setattr(
        "app.workers.device_provision_task.provision_from_feishu.apply_async",
        lambda *args, **kwargs: calls.__setitem__("provision", calls["provision"] + 1),
        raising=False,
    )
    monkeypatch.setattr(authorized_events, "enqueue_reply", lambda message_id, text: replies.append((message_id, text)))

    with Session(_engine()) as db:
        result = dispatch_authorized_event(
            db,
            payload=_message_payload(
                "单通无声，请排查 sn=SN-1 ip=10.0.0.1",
                open_id="ou-unmapped",
            ),
        )
        assert result["handled"] == "permission_denied"
        assert result["identity_status"] == "PENDING_MAPPING"
        assert calls["provision"] == 0
        assert replies


def test_viewer_cannot_stop_reproduction_and_no_task_is_enqueued(monkeypatch):
    monkeypatch.setattr(settings, "feishu_identity_rbac_enabled", True)
    calls = {"cancel": 0}
    monkeypatch.setattr(
        authorized_events, "enqueue_reply", lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "app.integrations.feishu.events.cancel_reproduction.apply_async",
        lambda *args, **kwargs: calls.__setitem__("cancel", calls["cancel"] + 1),
        raising=False,
    )

    with Session(_engine()) as db:
        case, _session = _case_session(db)
        _identity(db, open_id="ou-viewer", role="VIEWER")
        result = dispatch_authorized_event(
            db,
            payload=_message_payload(
                f"停止复现 {case.case_no}", open_id="ou-viewer", message_id="msg-viewer-stop",
            ),
        )
        assert result["handled"] == "permission_denied"
        assert result["capability"] == "CONTROL_REPRODUCTION"
        assert calls["cancel"] == 0


def test_engineer_can_stop_reproduction_after_authorization(monkeypatch):
    monkeypatch.setattr(settings, "feishu_identity_rbac_enabled", True)
    calls = {"cancel": 0}
    monkeypatch.setattr(
        "app.integrations.feishu.events.cancel_reproduction.apply_async",
        lambda *args, **kwargs: calls.__setitem__("cancel", calls["cancel"] + 1),
        raising=False,
    )
    monkeypatch.setattr("app.integrations.feishu.events.enqueue_reply", lambda *args, **kwargs: None)

    with Session(_engine()) as db:
        case, _session = _case_session(db)
        _identity(db, open_id="ou-engineer", role="ENGINEER")
        result = dispatch_authorized_event(
            db,
            payload=_message_payload(
                f"停止复现 {case.case_no}", open_id="ou-engineer", message_id="msg-engineer-stop",
            ),
        )
        assert result["handled"] == "stop_reproduction"
        assert calls["cancel"] == 1


def test_viewer_card_stop_is_denied_before_control_handler(monkeypatch):
    monkeypatch.setattr(settings, "feishu_identity_rbac_enabled", True)
    calls = {"cancel": 0}
    monkeypatch.setattr(
        "app.integrations.feishu.events.cancel_reproduction.apply_async",
        lambda *args, **kwargs: calls.__setitem__("cancel", calls["cancel"] + 1),
        raising=False,
    )

    with Session(_engine()) as db:
        _case, session = _case_session(db)
        _identity(db, open_id="ou-viewer", role="VIEWER")
        payload = {
            "header": {"event_type": "card.action.trigger", "event_id": "evt-card", "tenant_key": "tenant-a"},
            "operator": {"open_id": "ou-viewer"},
            "event": {"action": {"value": {"action": "STOP_REPRODUCTION", "session_id": session.id}}},
        }
        result = dispatch_authorized_event(db, payload=payload)
        assert result["handled"] == "permission_denied"
        assert result["toast"]["type"] == "error"
        assert calls["cancel"] == 0


def test_websocket_card_payload_preserves_tenant_for_rbac():
    from app.integrations.feishu.long_connection import _card_action_payload

    data = SimpleNamespace(
        header=SimpleNamespace(event_id="evt-card-sdk", tenant_key="tenant-sdk", create_time="123"),
        event=SimpleNamespace(
            action=SimpleNamespace(value={"action": "OPEN_CASE", "case_id": "case-1"}),
            operator=SimpleNamespace(open_id="ou-sdk"),
            context=SimpleNamespace(open_chat_id="oc-sdk"),
        ),
    )
    payload = _card_action_payload(data)
    assert payload["header"]["tenant_key"] == "tenant-sdk"
    assert payload["operator"]["open_id"] == "ou-sdk"
    assert payload["event"]["chat_id"] == "oc-sdk"
