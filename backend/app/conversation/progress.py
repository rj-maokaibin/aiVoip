from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.conversation.response import GroundedConversationResponder
from app.conversation.snapshot import ConversationSnapshotBuilder
from app.conversation.state_service import ConversationStateService
from app.db.models import FeishuCaseBinding


_MEANINGFUL_TERMINAL = {"DIAGNOSED", "ROOT_CAUSE_CONFIRMED", "RESOLVED", "CLOSED", "FAILED"}


def _digest_payload(snapshot: dict[str, Any]) -> dict[str, Any]:
    diagnosis = snapshot.get("diagnosis") or {}
    runtime = snapshot.get("runtime") or {}
    case = snapshot.get("case") or {}
    return {
        "case_status": case.get("status"),
        "headline": diagnosis.get("headline"),
        "known": list(diagnosis.get("known") or [])[:8],
        "unknown": list(diagnosis.get("unknown") or [])[:8],
        "blocking_reason": diagnosis.get("blocking_reason"),
        "manual_action": diagnosis.get("manual_action"),
        "has_running_work": bool(runtime.get("has_running_work")),
        "reproduction_state": runtime.get("reproduction_state"),
    }


def _is_user_meaningful(snapshot: dict[str, Any]) -> bool:
    case = snapshot.get("case") or {}
    diagnosis = snapshot.get("diagnosis") or {}
    if case.get("status") in _MEANINGFUL_TERMINAL:
        return True
    if diagnosis.get("headline") or diagnosis.get("blocking_reason"):
        return True
    if diagnosis.get("known"):
        return True
    return False


def push_meaningful_progress(db: Session, *, case_id: str) -> dict[str, Any]:
    """Push one grounded progress update only when user-visible truth changed.

    Cycle numbers, duplicate card refreshes and transport retries are intentionally
    absent from the digest.  This function has no diagnosis/device authority; it
    only turns an already-persisted Case snapshot into a Feishu text update.
    """
    snapshot = ConversationSnapshotBuilder().build(db, case_id)
    payload = _digest_payload(snapshot)
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()

    state_service = ConversationStateService()
    _conversation, state = state_service.get_or_create(db, case_id=case_id)
    previous = str(state.last_progress_digest or "")
    if previous == digest:
        return {"status": "SKIPPED", "reason": "NO_MEANINGFUL_CHANGE", "digest": digest}

    # Persist the digest even when the first snapshot is not interesting so a
    # later analyzer finding can be recognized as the first meaningful change.
    state.last_progress_digest = digest
    db.flush()
    if not _is_user_meaningful(snapshot):
        return {"status": "SKIPPED", "reason": "NO_USER_VISIBLE_FINDING", "digest": digest}

    binding = db.scalar(select(FeishuCaseBinding).where(
        FeishuCaseBinding.case_id == case_id,
        FeishuCaseBinding.status == "ACTIVE",
    ).limit(1))
    if binding is None or not binding.source_message_id:
        return {"status": "SKIPPED", "reason": "NO_SOURCE_MESSAGE", "digest": digest}

    text = GroundedConversationResponder().render(
        db, case_id=case_id, intent="CASE_PROGRESS_QUERY"
    )
    if not text:
        return {"status": "SKIPPED", "reason": "EMPTY_PROGRESS_TEXT", "digest": digest}
    from app.integrations.feishu.feedback import enqueue_reply
    queued = enqueue_reply(binding.source_message_id, text)
    return {
        "status": "QUEUED" if queued else "SKIPPED",
        "reason": "MEANINGFUL_CHANGE" if queued else "FEISHU_REPLY_NOT_QUEUED",
        "digest": digest,
    }
