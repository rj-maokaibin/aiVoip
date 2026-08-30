from __future__ import annotations

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.conversation.state_service import ConversationStateService
from app.db.base import Base
from app.db import models as _models  # noqa: F401
from app.db import conversation_models as _conversation_models  # noqa: F401
from app.db.models import Case, Evidence, FeishuCaseBinding
from app.integrations.feishu.case_boundary import (
    arm_current_case_once,
    attachment_matches_active_question,
    consume_current_case_once,
    create_and_activate_new_case,
    is_explicit_continue_current_case,
    is_explicit_new_case,
    is_pure_continue_current_case_command,
    is_pure_new_case_command,
)
from app.integrations.feishu.case_resolver import active_case_for_chat
from app.integrations.feishu.service import bind_case_to_chat


def _engine():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return engine


def _source(*, message_id: str, text: str = "") -> dict:
    return {
        "tenant_key": "tenant-a",
        "chat_id": "chat-a",
        "message_id": message_id,
        "sender_open_id": "ou-test",
        "chat_type": "group",
        "normalized_text": text,
        "attachments": [],
    }


def _case_with_binding(db, *, case_no: str = "CASE-BOUNDARY-A"):
    case = Case(case_no=case_no, summary="旧故障：首位按键丢失", status="WAITING_USER")
    db.add(case)
    db.flush()
    binding = bind_case_to_chat(
        db,
        case_id=case.id,
        chat_id="chat-a",
        chat_type="group",
        source_context=_source(message_id="msg-old", text="首位按键丢失，请分析"),
    )
    db.flush()
    return case, binding


def test_case_boundary_phrase_classification_is_explicit_not_incidental():
    assert is_explicit_new_case("这是另一个故障，请重新分析") is True
    assert is_explicit_new_case("新建 Case") is True
    assert is_pure_new_case_command(" 新建 Case。 ") is True
    assert is_explicit_continue_current_case("继续当前 Case") is True
    assert is_pure_continue_current_case_command("继续当前 Case。") is True
    assert is_explicit_new_case("继续分析当前故障") is False
    assert is_explicit_continue_current_case("这是新的故障") is False
    assert is_explicit_new_case("这是新的抓包，请继续分析") is False
    assert is_explicit_new_case("这是另一个录音文件") is False


def test_same_chat_case_switch_preserves_old_case_and_evidence_and_resets_state():
    engine = _engine()
    Local = sessionmaker(bind=engine, expire_on_commit=False)

    with Local() as db:
        old_case, old_binding = _case_with_binding(db)
        old_evidence = Evidence(
            case_id=old_case.id,
            type="PCAP",
            source="FEISHU_UPLOAD",
            filename="old.pcap",
            object_key="case-boundary/old.pcap",
            size_bytes=123,
            sha256="a" * 64,
        )
        db.add(old_evidence)
        _conversation, state = ConversationStateService().get_or_create(
            db, case_id=old_case.id, source_context=_source(message_id="msg-state")
        )
        state.active_question_json = {
            "need": "anomaly_timestamp",
            "text": "请提供异常时间",
        }
        state.slots_json = {
            "anomaly_timestamp": {"state": "UNKNOWN_BY_USER", "value": None}
        }
        state.unavailable_needs_json = ["device_access"]
        state.last_user_intent = "ANSWER_ACTIVE_QUESTION"
        state.last_progress_digest = "old-progress"
        state.material_context_hash = "old-material"
        db.flush()

        result = create_and_activate_new_case(
            db,
            current_case=old_case,
            current_binding_id=old_binding.id,
            chat_id="chat-a",
            chat_type="group",
            source_context=_source(
                message_id="msg-new",
                text="这是新的故障，现场出现无声，请分析",
            ),
            text="这是新的故障，现场出现无声，请分析",
            attachments=[],
            actor="feishu:ou-test",
        )
        db.flush()

        new_case = result.new_case
        assert new_case.id != old_case.id
        assert old_case.status == "WAITING_USER"
        assert db.get(Evidence, old_evidence.id) is not None
        assert db.get(Evidence, old_evidence.id).case_id == old_case.id

        old_binding_after = db.get(FeishuCaseBinding, old_binding.id)
        new_binding = db.scalar(
            select(FeishuCaseBinding).where(FeishuCaseBinding.case_id == new_case.id)
        )
        assert old_binding_after.status == "CLOSED"
        assert new_binding is not None
        assert new_binding.status == "ACTIVE"

        active_case, active_binding_id = active_case_for_chat(
            db, tenant_key="tenant-a", chat_id="chat-a"
        )
        assert active_case is not None
        assert active_case.id == new_case.id
        assert active_binding_id == new_binding.id

        conversation, new_state = ConversationStateService().case_state(db, new_case.id)
        assert conversation is not None
        assert conversation.active_case_id == new_case.id
        assert new_state.active_question_json is None
        assert new_state.slots_json == {}
        assert new_state.unavailable_needs_json == []
        assert new_state.last_user_intent is None
        assert new_state.last_progress_digest is None
        assert new_state.material_context_hash is None

        old_state_view = ConversationStateService().case_state(db, old_case.id)
        assert old_state_view == (None, None)


def test_continue_confirmation_is_one_shot_and_case_scoped():
    engine = _engine()
    Local = sessionmaker(bind=engine, expire_on_commit=False)

    with Local() as db:
        case, _binding = _case_with_binding(db)
        source = _source(message_id="msg-confirm")
        arm_current_case_once(db, case_id=case.id, source_context=source)
        assert consume_current_case_once(db, case_id=case.id, source_context=source) is True
        assert consume_current_case_once(db, case_id=case.id, source_context=source) is False


def test_requested_pcap_attachment_is_safe_follow_up_but_unrequested_file_is_not():
    engine = _engine()
    Local = sessionmaker(bind=engine, expire_on_commit=False)

    with Local() as db:
        case, _binding = _case_with_binding(db)
        ConversationStateService().mark_question_asked(
            db,
            case_id=case.id,
            text="请上传 PCAP/PCAPNG 抓包",
            need="pcap",
        )
        assert attachment_matches_active_question(
            db,
            case_id=case.id,
            attachments=[{
                "file_key": "fk-pcap",
                "filename": "tcpdump-new.pcap",
                "message_type": "file",
            }],
        ) is True
        assert attachment_matches_active_question(
            db,
            case_id=case.id,
            attachments=[{
                "file_key": "fk-zip",
                "filename": "unrelated.zip",
                "message_type": "file",
            }],
        ) is False


def test_ambiguous_new_pcap_is_blocked_before_case_or_evidence_write(monkeypatch):
    from app.integrations.feishu import events

    engine = _engine()
    Local = sessionmaker(bind=engine, expire_on_commit=False)
    replies: list[tuple[str, str]] = []
    monkeypatch.setattr(events, "enqueue_reply", lambda message_id, text: replies.append((message_id, text)))

    with Local() as db:
        old_case, _binding = _case_with_binding(db)
        old_case_id = old_case.id
        db.commit()

    payload = {
        "header": {
            "event_type": "im.message.receive_v1",
            "event_id": "evt-new-pcap",
            "tenant_key": "tenant-a",
        },
        "event": {
            "chat_id": "chat-a",
            "chat_type": "group",
            "sender": {"sender_id": {"open_id": "ou-test"}},
            "message": {
                "message_id": "msg-new-pcap",
                "message_type": "file",
                "content": {
                    "file_key": "fk-new-pcap",
                    "file_name": "another-fault.pcap",
                },
            },
        },
    }

    with Local() as db:
        result = events.dispatch_event(db, payload=payload, actor="feishu:ou-test")
        db.commit()
        assert result["handled"] == "needs_case_boundary_confirmation"
        assert result["case_id"] == old_case_id
        assert result["missing_user_inputs"] == ["continue_current_case_or_new_case"]
        assert db.scalar(select(func.count(Case.id))) == 1
        assert db.scalar(select(func.count(Evidence.id))) == 0
        active_case, _ = active_case_for_chat(db, tenant_key="tenant-a", chat_id="chat-a")
        assert active_case is not None and active_case.id == old_case_id

    assert len(replies) == 1
    assert "继续当前 Case" in replies[0][1]
    assert "新建 Case" in replies[0][1]
    assert "不会写入任何 Case" in replies[0][1]
