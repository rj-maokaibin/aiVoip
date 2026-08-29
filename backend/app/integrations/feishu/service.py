from __future__ import annotations

import logging

from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import FeishuCaseBinding
from app.integrations.feishu.cards import FeishuCaseCardBuilder
from app.integrations.feishu.case_resolver import (
    activate_binding_lifecycle,
    active_case_for_chat,
    normalize_tenant_key,
)
from app.integrations.feishu.transport import FeishuLiveTransport


log = logging.getLogger(__name__)
_CONFLICT_INFO_KEY = "feishu_active_case_conflict"


class FeishuActiveCaseConflict(RuntimeError):
    def __init__(self, *, chat_id: str, existing_case_id: str):
        self.chat_id = chat_id
        self.existing_case_id = existing_case_id
        super().__init__(f"FEISHU_CHAT_ACTIVE_CASE_CONFLICT:{chat_id}:{existing_case_id}")


class FeishuCaseAlreadyBound(RuntimeError):
    def __init__(self, *, case_id: str, existing_chat_id: str):
        self.case_id = case_id
        self.existing_chat_id = existing_chat_id
        super().__init__(f"FEISHU_CASE_ALREADY_BOUND:{case_id}:{existing_chat_id}")


@event.listens_for(Session, "before_commit")
def _reject_commit_after_active_case_conflict(session: Session) -> None:
    """Make a swallowed G1 conflict rollback-only without rolling back for callers.

    Some legacy workers intentionally catch best-effort Feishu binding exceptions.
    For an Active Case conflict, continuing and committing would persist a newly
    created but unbound loser Case. Marking only this Session transaction blocks
    that commit while preserving the caller's right to decide the rollback scope.
    """
    payload = session.info.get(_CONFLICT_INFO_KEY)
    if payload:
        raise FeishuActiveCaseConflict(
            chat_id=str(payload["chat_id"]),
            existing_case_id=str(payload["existing_case_id"]),
        )


@event.listens_for(Session, "after_rollback")
def _clear_active_case_conflict_after_rollback(session: Session) -> None:
    session.info.pop(_CONFLICT_INFO_KEY, None)


@event.listens_for(Session, "after_soft_rollback")
def _clear_active_case_conflict_after_soft_rollback(session: Session, _previous_transaction) -> None:
    if not session.in_transaction():
        session.info.pop(_CONFLICT_INFO_KEY, None)


def _raise_active_case_conflict(db: Session, *, chat_id: str, existing_case_id: str) -> None:
    db.info[_CONFLICT_INFO_KEY] = {
        "chat_id": chat_id,
        "existing_case_id": existing_case_id,
    }
    raise FeishuActiveCaseConflict(chat_id=chat_id, existing_case_id=existing_case_id)


def bind_case_to_chat(db: Session, *, case_id: str, chat_id: str, chat_type: str | None = None,
                      receive_id_type: str | None = None,
                      source_context: dict | None = None) -> FeishuCaseBinding | None:
    """Bind one Case to one Feishu conversation under the G1 governance contract."""
    if receive_id_type is None:
        receive_id_type = 'chat_id'
    if not chat_id:
        return None
    source_context = source_context or {}
    tenant_key = normalize_tenant_key(source_context.get('tenant_key'))
    created_by_open_id = source_context.get('sender_open_id')

    def apply_source_context(row: FeishuCaseBinding) -> None:
        row.source_event_id = row.source_event_id or source_context.get('event_id')
        row.source_message_id = row.source_message_id or source_context.get('message_id')
        row.source_root_message_id = row.source_root_message_id or source_context.get('root_message_id')
        row.source_parent_message_id = row.source_parent_message_id or source_context.get('parent_message_id')
        row.source_sender_open_id = row.source_sender_open_id or created_by_open_id
        row.source_chat_type = row.source_chat_type or chat_type or source_context.get('chat_type')
        row.source_tenant_key = row.source_tenant_key or tenant_key
        row.source_message_timestamp = row.source_message_timestamp or source_context.get('create_time')
        row.source_normalized_text = row.source_normalized_text or source_context.get('normalized_text')
        row.source_attachment_refs = row.source_attachment_refs or source_context.get('attachments')

    binding = db.scalar(select(FeishuCaseBinding).where(FeishuCaseBinding.case_id == case_id).limit(1))
    if binding is not None:
        if binding.receive_id_type == 'chat_id' and binding.receive_id and binding.receive_id != chat_id:
            raise FeishuCaseAlreadyBound(case_id=case_id, existing_chat_id=binding.receive_id)
        apply_source_context(binding)
        binding.receive_id = chat_id
        binding.receive_id_type = receive_id_type
        db.flush()
        if receive_id_type == 'chat_id':
            active_case, _ = active_case_for_chat(db, tenant_key=tenant_key, chat_id=chat_id)
            if active_case is not None and active_case.id != case_id:
                _raise_active_case_conflict(db, chat_id=chat_id, existing_case_id=active_case.id)
            activate_binding_lifecycle(
                db, binding_id=binding.id, tenant_key=tenant_key, chat_id=chat_id,
                created_by_open_id=created_by_open_id,
            )
        return binding

    if receive_id_type == 'chat_id':
        active_case, _ = active_case_for_chat(db, tenant_key=tenant_key, chat_id=chat_id)
        if active_case is not None and active_case.id != case_id:
            _raise_active_case_conflict(db, chat_id=chat_id, existing_case_id=active_case.id)

    candidate = FeishuCaseBinding(
        case_id=case_id, receive_id=chat_id, receive_id_type=receive_id_type,
        message_id=None, status='ACTIVE', card_version=0,
    )
    apply_source_context(candidate)
    try:
        with db.begin_nested():
            db.add(candidate)
            db.flush()
    except IntegrityError:
        if receive_id_type == 'chat_id':
            active_case, _ = active_case_for_chat(db, tenant_key=tenant_key, chat_id=chat_id)
            if active_case is not None and active_case.id != case_id:
                _raise_active_case_conflict(db, chat_id=chat_id, existing_case_id=active_case.id)
        existing = db.scalar(select(FeishuCaseBinding).where(FeishuCaseBinding.case_id == case_id).limit(1))
        if existing is not None:
            return existing
        raise

    if receive_id_type == 'chat_id':
        activate_binding_lifecycle(
            db, binding_id=candidate.id, tenant_key=tenant_key, chat_id=chat_id,
            created_by_open_id=created_by_open_id,
        )
    db.flush()
    return candidate


class FeishuCaseCardService:
    async def sync_case_card(self, db: Session, *, case_id: str, receive_id: str | None = None,
                             receive_id_type: str | None = None) -> FeishuCaseBinding:
        if not settings.feishu_live_enabled:
            raise ValueError("FEISHU_LIVE_DISABLED")
        built = FeishuCaseCardBuilder().build(db, case_id)
        binding = db.scalar(select(FeishuCaseBinding).where(FeishuCaseBinding.case_id == case_id).limit(1))
        rid = receive_id or (binding.receive_id if binding else "") or settings.feishu_default_receive_id
        rtype = receive_id_type or (binding.receive_id_type if binding else "") or settings.feishu_receive_id_type
        if not rid:
            raise ValueError("FEISHU_RECEIVE_ID_NOT_CONFIGURED")
        transport = FeishuLiveTransport()
        if binding and binding.message_id:
            await transport.update_card(message_id=binding.message_id, card=built.card)
            binding.receive_id = rid
            binding.receive_id_type = rtype
            binding.status = "ACTIVE"
            binding.card_version += 1
        else:
            result = await transport.send_card(receive_id=rid, receive_id_type=rtype, card=built.card)
            if binding is None:
                binding = db.scalar(select(FeishuCaseBinding).where(FeishuCaseBinding.case_id == case_id).limit(1))
            if binding is None:
                candidate = FeishuCaseBinding(
                    case_id=case_id, receive_id=rid, receive_id_type=rtype,
                    message_id=result.message_id, status="ACTIVE", card_version=1,
                )
                try:
                    with db.begin_nested():
                        db.add(candidate)
                        db.flush()
                except IntegrityError:
                    binding = db.scalar(select(FeishuCaseBinding).where(FeishuCaseBinding.case_id == case_id).limit(1))
                    if binding is not None and not binding.message_id:
                        binding.message_id = result.message_id
                        binding.status = "ACTIVE"
                else:
                    binding = candidate
            else:
                binding.receive_id = rid
                binding.receive_id_type = rtype
                binding.message_id = result.message_id
                binding.status = "ACTIVE"
                binding.card_version += 1
        db.flush()

        # Card refresh is already the event fan-in for analyzer/diagnosis/repro
        # milestones. Piggyback a text push only when the grounded user-visible
        # digest changed; duplicate/cycle-only refreshes remain silent.
        if settings.conversation_cycle_decoupled:
            try:
                from app.conversation.progress import push_meaningful_progress
                push_meaningful_progress(db, case_id=case_id)
            except Exception:
                log.exception('meaningful conversation progress push failed case=%s', case_id)
        db.flush()
        return binding
