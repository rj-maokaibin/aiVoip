from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.contracts.enums import CaseStatus
from app.core.ids import new_case_no
from app.db.models import Case, CaseStateHistory
from app.integrations.feishu.case_resolver import close_binding_lifecycle


_BOUNDARY_SLOT = "__case_boundary__"
_ALLOW_CURRENT_ONCE = "ALLOW_CURRENT_CASE_ONCE"

_NEW_CASE_PHRASES = (
    "新建 case", "新建case", "创建 case", "创建case", "另开 case", "另开case",
    "开新 case", "开新case", "新 case", "新case", "new case", "another case",
    "新问题", "新的问题", "另一个问题", "另外一个问题", "新故障", "新的故障",
    "另一个故障", "另外一个故障",
)
# Case-boundary continuation is deliberately restricted to phrases that identify
# the *current Case/problem/fault*. Generic analysis controls such as `继续分析`
# belong to the Conversation layer (CONTINUE_ANALYSIS) and must not arm a
# one-shot Case-boundary confirmation.
_CONTINUE_CASE_PHRASES = (
    "继续当前 case", "继续当前case", "继续这个 case", "继续这个case",
    "继续当前问题", "继续这个问题", "继续当前故障", "继续这个故障",
    "继续当前诊断", "继续这个诊断", "continue current case",
)
_PURE_NEW_COMMANDS = {
    "新建case", "创建case", "另开case", "开新case", "新case", "newcase", "anothercase",
}
_PURE_CONTINUE_COMMANDS = {
    "继续当前case", "继续这个case", "继续当前问题", "继续这个问题",
    "继续当前故障", "继续这个故障", "继续当前诊断", "继续这个诊断",
    "continuecurrentcase",
}


@dataclass(frozen=True)
class CaseSwitchResult:
    old_case_id: str
    old_case_no: str
    new_case: Case


def _compact_command(text: str) -> str:
    value = str(text or "").strip().lower()
    value = re.sub(r"[\s\u3000]+", "", value)
    value = re.sub(r"[。！？!?，,；;：:\.]+$", "", value)
    return value


def is_explicit_new_case(text: str) -> bool:
    lowered = str(text or "").strip().lower()
    return any(phrase in lowered for phrase in _NEW_CASE_PHRASES)


def is_pure_new_case_command(text: str) -> bool:
    return _compact_command(text) in _PURE_NEW_COMMANDS


def is_explicit_continue_current_case(text: str) -> bool:
    lowered = str(text or "").strip().lower()
    return any(phrase in lowered for phrase in _CONTINUE_CASE_PHRASES)


def is_pure_continue_current_case_command(text: str) -> bool:
    return _compact_command(text) in _PURE_CONTINUE_COMMANDS


def _new_case_summary(text: str, attachments: list[dict[str, Any]] | None) -> str:
    text = str(text or "").strip()
    if text and not is_pure_new_case_command(text):
        return text[:1000]
    names = [
        str(item.get("filename") or "").strip()
        for item in (attachments or [])
        if str(item.get("filename") or "").strip()
    ]
    if names:
        return f"飞书新故障：{', '.join(names[:5])}"[:1000]
    return "飞书新故障（待补充现象）"


def _reset_conversation_for_case(
    db: Session, *, case_id: str, source_context: dict[str, Any]
) -> None:
    """Reset Case-scoped mutable state while retaining immutable chat turn history."""
    from app.conversation.state_service import ConversationStateService

    conversation, state = ConversationStateService().get_or_create(
        db, case_id=case_id, source_context=source_context
    )
    conversation.active_case_id = case_id
    conversation.active_topic = None
    conversation.entities_json = {}
    state.active_question_json = None
    state.slots_json = {}
    state.unavailable_needs_json = []
    state.last_user_intent = None
    state.last_progress_digest = None
    state.material_context_hash = None
    db.flush()


def create_and_activate_new_case(
    db: Session,
    *,
    current_case: Case,
    current_binding_id: str,
    chat_id: str,
    chat_type: str | None,
    source_context: dict[str, Any],
    text: str,
    attachments: list[dict[str, Any]] | None,
    actor: str,
) -> CaseSwitchResult:
    """Atomically rotate one chat from Case A to Case B without closing Case A."""
    if not current_case or not current_binding_id or not chat_id:
        raise ValueError("CASE_BOUNDARY_ACTIVE_BINDING_REQUIRED")

    from app.integrations.feishu.service import bind_case_to_chat
    from app.services.audit import audit

    # A pure boundary command is governance, not diagnostic context. Keep its
    # source identity on the binding but do not materialize it as the new Case's
    # initial diagnostic ConversationTurn.
    binding_context = dict(source_context or {})
    if is_pure_new_case_command(text):
        binding_context["normalized_text"] = ""
        binding_context["attachments"] = []

    with db.begin_nested():
        new_case = Case(
            case_no=new_case_no(),
            summary=_new_case_summary(text, attachments),
            status=CaseStatus.NEW.value,
            created_by="feishu-case-boundary",
        )
        db.add(new_case)
        db.flush()
        db.add(CaseStateHistory(
            case_id=new_case.id,
            from_status=None,
            to_status=CaseStatus.NEW.value,
            event="CASE_BOUNDARY_NEW_CASE",
            actor=actor,
            reason=f"superseded active chat binding from {current_case.case_no}",
            context_json={
                "previous_case_id": current_case.id,
                "previous_case_no": current_case.case_no,
                "chat_id": chat_id,
            },
        ))
        close_binding_lifecycle(
            db,
            binding_id=current_binding_id,
            reason="SUPERSEDED_BY_NEW_CASE",
        )
        bind_case_to_chat(
            db,
            case_id=new_case.id,
            chat_id=chat_id,
            chat_type=chat_type,
            source_context=binding_context,
        )
        _reset_conversation_for_case(db, case_id=new_case.id, source_context=source_context)
        audit(
            db,
            case_id=new_case.id,
            actor=actor,
            event_type="FEISHU_ACTIVE_CASE_SWITCHED",
            target_type="case",
            target_id=new_case.id,
            detail={
                "previous_case_id": current_case.id,
                "previous_case_no": current_case.case_no,
                "new_case_no": new_case.case_no,
                "chat_id": chat_id,
                "binding_close_reason": "SUPERSEDED_BY_NEW_CASE",
            },
        )
        db.flush()

    return CaseSwitchResult(
        old_case_id=current_case.id,
        old_case_no=current_case.case_no,
        new_case=new_case,
    )


def arm_current_case_once(
    db: Session,
    *,
    case_id: str,
    source_context: dict[str, Any],
) -> None:
    from app.conversation.state_service import ConversationStateService

    _conversation, state = ConversationStateService().get_or_create(
        db, case_id=case_id, source_context=source_context
    )
    slots = dict(state.slots_json or {})
    slots[_BOUNDARY_SLOT] = {
        "state": _ALLOW_CURRENT_ONCE,
        "case_id": case_id,
        "source": "USER_CASE_BOUNDARY_CONFIRMATION",
    }
    state.slots_json = slots
    db.flush()


def consume_current_case_once(
    db: Session,
    *,
    case_id: str,
    source_context: dict[str, Any],
) -> bool:
    from app.conversation.state_service import ConversationStateService

    _conversation, state = ConversationStateService().get_or_create(
        db, case_id=case_id, source_context=source_context
    )
    slots = dict(state.slots_json or {})
    marker = dict(slots.get(_BOUNDARY_SLOT) or {})
    if marker.get("state") != _ALLOW_CURRENT_ONCE or marker.get("case_id") != case_id:
        return False
    slots.pop(_BOUNDARY_SLOT, None)
    state.slots_json = slots
    db.flush()
    return True


def attachment_matches_active_question(
    db: Session,
    *,
    case_id: str,
    attachments: list[dict[str, Any]] | None,
) -> bool:
    if not attachments:
        return False
    from app.conversation.state_service import ConversationStateService

    _conversation, state = ConversationStateService().case_state(db, case_id)
    if state is None:
        return False
    question = dict(state.active_question_json or {})
    need = str(question.get("need") or question.get("slot_key") or "").lower()
    if not need:
        return False

    names = [str(item.get("filename") or "").lower() for item in attachments]
    types = [str(item.get("message_type") or "").lower() for item in attachments]
    if need == "pcap":
        return any(name.endswith((".pcap", ".pcapng")) for name in names)
    if need == "recording":
        return any(t == "audio" for t in types) or any(
            name.endswith((".wav", ".mp3", ".m4a", ".aac", ".ogg", ".flac"))
            for name in names
        )
    return False
