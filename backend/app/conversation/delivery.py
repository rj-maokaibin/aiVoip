from __future__ import annotations

import hashlib

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.conversation_models import FeishuReplyDeliveryTrace
from app.db.models import FeishuCaseBinding


def semantic_reply_key(message_id: str, text: str) -> str:
    return hashlib.sha256(f"{message_id}\x1f{text}".encode("utf-8")).hexdigest()


def get_or_create_reply_trace(db: Session, *, message_id: str, text: str) -> FeishuReplyDeliveryTrace:
    key = semantic_reply_key(message_id, text)
    row = db.scalar(
        select(FeishuReplyDeliveryTrace)
        .where(
            FeishuReplyDeliveryTrace.source_message_id == message_id,
            FeishuReplyDeliveryTrace.semantic_key == key,
        )
        .order_by(FeishuReplyDeliveryTrace.created_at.desc())
        .limit(1)
    )
    if row is not None:
        return row
    binding = db.scalar(
        select(FeishuCaseBinding)
        .where(FeishuCaseBinding.source_message_id == message_id)
        .order_by(FeishuCaseBinding.created_at.desc())
        .limit(1)
    )
    row = FeishuReplyDeliveryTrace(
        case_id=binding.case_id if binding else None,
        source_message_id=message_id,
        semantic_key=key,
        stage="ENQUEUED",
        attempt_count=0,
    )
    db.add(row)
    db.flush()
    return row


def mark_reply_attempt(db: Session, row: FeishuReplyDeliveryTrace) -> None:
    row.attempt_count = int(row.attempt_count or 0) + 1
    row.stage = "SENDING"
    row.error_code = None
    row.last_error = None
    db.flush()


def mark_reply_sent(db: Session, row: FeishuReplyDeliveryTrace, sent_message_id: str | None) -> None:
    row.stage = "SENT"
    row.sent_message_id = sent_message_id
    row.error_code = None
    row.last_error = None
    db.flush()


def mark_reply_failed(db: Session, row: FeishuReplyDeliveryTrace, exc: Exception, *, retryable: bool) -> None:
    row.stage = "FAILED_RETRYABLE" if retryable else "FAILED"
    row.error_code = type(exc).__name__[:128]
    row.last_error = str(exc)[:2000]
    db.flush()
