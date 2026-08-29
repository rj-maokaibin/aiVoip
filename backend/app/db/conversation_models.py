from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.ids import new_id
from app.db.base import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Conversation(Base):
    """Channel-level conversational context, intentionally separate from Case.

    A Feishu conversation may exist without a Case for product/knowledge Q&A and
    may bind to different Cases over its lifetime. ``active_case_id`` is only the
    current conversational focus; formal Case authority remains in the existing
    Feishu Case resolver/binding layer.
    """

    __tablename__ = "conversations"
    __table_args__ = (
        UniqueConstraint("tenant_key", "channel", "chat_id", name="uq_conversation_channel_chat"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_key: Mapped[str] = mapped_column(String(256), default="", index=True)
    channel: Mapped[str] = mapped_column(String(32), default="FEISHU", index=True)
    chat_id: Mapped[str] = mapped_column(String(256), index=True)
    active_case_id: Mapped[str | None] = mapped_column(
        ForeignKey("cases.id", ondelete="SET NULL"), nullable=True, index=True
    )
    active_topic: Mapped[str | None] = mapped_column(String(256), nullable=True)
    entities_json: Mapped[dict] = mapped_column(JSON, default=dict)
    turn_no: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(24), default="ACTIVE", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ConversationState(Base):
    """Mutable conversational state used for active question/slot dedupe.

    ``slots_json`` stores a bounded map of slot -> state/value/asked_count.  The
    diagnostic engine does not consume this table directly; only materialized,
    explicitly classified diagnostic context is promoted to Evidence.
    """

    __tablename__ = "conversation_states"
    __table_args__ = (UniqueConstraint("conversation_id", name="uq_conversation_state_conversation"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), unique=True, index=True
    )
    active_question_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    slots_json: Mapped[dict] = mapped_column(JSON, default=dict)
    unavailable_needs_json: Mapped[list] = mapped_column(JSON, default=list)
    last_user_intent: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    last_progress_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    material_context_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ConversationTurn(Base):
    """Immutable conversational audit record.

    Chat-only/control/knowledge turns live here instead of being forced into the
    diagnostic Evidence table.  ``material_diagnostic_context`` is the explicit
    bridge that permits a turn to be promoted into technical Evidence.
    """

    __tablename__ = "conversation_turns"
    __table_args__ = (
        UniqueConstraint("conversation_id", "source_message_id", name="uq_conversation_turn_source"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    case_id: Mapped[str | None] = mapped_column(
        ForeignKey("cases.id", ondelete="SET NULL"), nullable=True, index=True
    )
    source_message_id: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)
    sender_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    direction: Mapped[str] = mapped_column(String(16), default="USER", index=True)
    text: Mapped[str] = mapped_column(Text)
    intent: Mapped[str] = mapped_column(String(64), index=True)
    classification: Mapped[str] = mapped_column(String(32), index=True)
    route_mode: Mapped[str] = mapped_column(String(32), index=True)
    material_diagnostic_context: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    parsed_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    evidence_id: Mapped[str | None] = mapped_column(
        ForeignKey("evidences.id", ondelete="SET NULL"), nullable=True, index=True
    )
    model_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    snapshot_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class FeishuReplyDeliveryTrace(Base):
    """Delivery-state trace for user-visible Feishu replies."""

    __tablename__ = "feishu_reply_delivery_traces"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    conversation_id: Mapped[str | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    case_id: Mapped[str | None] = mapped_column(
        ForeignKey("cases.id", ondelete="SET NULL"), nullable=True, index=True
    )
    source_message_id: Mapped[str] = mapped_column(String(256), index=True)
    semantic_key: Mapped[str] = mapped_column(String(64), index=True)
    stage: Mapped[str] = mapped_column(String(32), default="ENQUEUED", index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    sent_message_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
