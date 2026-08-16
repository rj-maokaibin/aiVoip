from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import FeishuCaseBinding
from app.integrations.feishu.cards import FeishuCaseCardBuilder
from app.integrations.feishu.transport import FeishuLiveTransport


def bind_case_to_chat(db: Session, *, case_id: str, chat_id: str, chat_type: str | None = None,
                      receive_id_type: str | None = None,
                      source_context: dict | None = None) -> FeishuCaseBinding | None:
    """Record that a Case belongs to a specific Feishu conversation (where the
    engineer @bot'ed / DM'ed it). Called at provision time so every conclusion
    card is pushed back to the SAME source conversation, even when different
    faults come from different groups. The binding's message_id stays None until
    the first card is actually sent (sync_case_card backfills it).

    ``receive_id_type`` defaults to 'chat_id' regardless of ``chat_type``: a
    p2p (DM) message's chat_id is the single-chat session id (oc_*) - the
    sender's open_id is NOT exposed there - so the conclusion card must still
    be sent with receive_id_type='chat_id' (same mechanism as a group), or
    Feishu rejects the send. An explicit ``receive_id_type`` always wins (e.g.
    the API caller binding a specific open_id target).

    Returns the binding, or None when chat_id is empty / the binding already
    exists with a message_id (already delivering).
    """
    if receive_id_type is None:
        receive_id_type = 'chat_id'
    if not chat_id:
        return None
    source_context = source_context or {}

    def apply_source_context(row: FeishuCaseBinding) -> None:
        # A binding represents the Case's original/main thread. Later correlated
        # messages must not move that anchor, otherwise replies to the original
        # card stop resolving. Follow-ups are stored separately as Evidence.
        row.source_event_id = row.source_event_id or source_context.get('event_id')
        row.source_message_id = row.source_message_id or source_context.get('message_id')
        row.source_root_message_id = row.source_root_message_id or source_context.get('root_message_id')
        row.source_parent_message_id = row.source_parent_message_id or source_context.get('parent_message_id')
        row.source_sender_open_id = row.source_sender_open_id or source_context.get('sender_open_id')
        row.source_chat_type = row.source_chat_type or chat_type or source_context.get('chat_type')
        row.source_tenant_key = row.source_tenant_key or source_context.get('tenant_key')
        row.source_message_timestamp = row.source_message_timestamp or source_context.get('create_time')
        row.source_normalized_text = row.source_normalized_text or source_context.get('normalized_text')
        row.source_attachment_refs = row.source_attachment_refs or source_context.get('attachments')

    binding = db.scalar(select(FeishuCaseBinding).where(FeishuCaseBinding.case_id == case_id).limit(1))
    if binding is not None:
        apply_source_context(binding)
        # Keep existing delivery target; never override a live message.
        if binding.message_id:
            return binding
        binding.receive_id = chat_id
        binding.receive_id_type = receive_id_type
        db.flush()
        return binding
    binding = FeishuCaseBinding(case_id=case_id, receive_id=chat_id, receive_id_type=receive_id_type,
                                message_id=None, status='ACTIVE', card_version=0)
    apply_source_context(binding)
    db.add(binding)
    db.flush()
    return binding


class FeishuCaseCardService:
    async def sync_case_card(self, db: Session, *, case_id: str, receive_id: str | None = None, receive_id_type: str | None = None) -> FeishuCaseBinding:
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
                binding = FeishuCaseBinding(case_id=case_id, receive_id=rid, receive_id_type=rtype, message_id=result.message_id, status="ACTIVE", card_version=1)
                db.add(binding)
            else:
                binding.receive_id = rid
                binding.receive_id_type = rtype
                binding.message_id = result.message_id
                binding.status = "ACTIVE"
                binding.card_version += 1
        db.flush()
        return binding
