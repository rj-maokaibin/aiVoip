"""Shared Feishu event dispatch used by both the HTTP callback and the WebSocket
long-connection listener.

Both transports normalize an incoming payload into the same header/event shape
and call dispatch_event, so provision / stop-reproduction / experiment actions
behave identically no matter how the event arrived (webhook vs long connection
-- the latter is used when the deployment has no public callback URL).
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import DiagnosticExperiment, ExperimentRun, ReproductionSession
from app.experiments.orchestrator import DiagnosticExperimentOrchestrator
from app.workers.reproduction_tasks import cancel_reproduction


def action_value(payload: dict) -> dict:
    """Extract the action value dict (card button callbacks)."""
    candidates = [
        payload.get("action"),
        (payload.get("event") or {}).get("action") if isinstance(payload.get("event"), dict) else None,
    ]
    for item in candidates:
        if isinstance(item, dict):
            value = item.get("value")
            if isinstance(value, dict):
                return value
    return {}


def callback_actor(payload: dict) -> str:
    for node in [payload.get("operator"),
                 (payload.get("event") or {}).get("operator") if isinstance(payload.get("event"), dict) else None]:
        if isinstance(node, dict):
            for key in ("open_id", "user_id", "union_id"):
                if node.get(key):
                    return f"feishu:{node[key]}"
    return "feishu:callback"


# Card action-trigger event types (both the v2 schema `card.action.trigger` and
# the legacy `card.action.trigger_v1`). For these the callback must answer with
# a toast (and optionally an updated card) to give the user immediate feedback.
CARD_ACTION_EVENT_TYPES = {"card.action.trigger", "card.action.trigger_v1"}


def _card_action_response(result: dict) -> dict:
    """Map an action result to a Feishu card-action callback response.

    Adds a user-visible ``toast`` and, when the action mutates the case, an
    ``updated_card`` so the card is refreshed immediately (both formats are
    supported by card.action.trigger / card.action.trigger_v1).
    """
    handled = result.get("handled")
    if handled == "error":
        toast = {"type": "error", "content": "操作失败：请稍后重试"}
    elif handled == "stop_reproduction":
        toast = {"type": "info", "content": "已请求安全停止自动复现"}
    elif handled == "external_action_completed":
        toast = {"type": "success", "content": "已记录现场操作完成"}
    elif handled == "open_case":
        toast = {"type": "info", "content": "请在网页端查看 Case 详情"}
    else:
        toast = {"type": "info", "content": "已收到操作请求"}
    out = {"handled": handled, "toast": toast}
    if handled in {"stop_reproduction", "external_action_completed"}:
        out["updated_card"] = True
    return out


def dispatch_event(db: Session, *, payload: dict, actor: str = "feishu:callback") -> dict:
    """Handle a normalized Feishu event payload (im.message.receive_v1 text /
    card actions). Returns an out-dict with a human-readable summary.

    actor is used for STOP_REPRODUCTION / EXTERNAL_ACTION_COMPLETED actions; the
    caller should pass the extracted operator when available.
    """
    header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
    event_type = str(header.get("event_type") or payload.get("type") or "")

    if event_type == "im.message.receive_v1":
        event = payload.get("event") or {}
        msg = event.get("message") or {}
        chat_id = str(event.get("chat_id") or msg.get("chat_id") or "")
        # 'group' -> chat_id is a chat_id; 'p2p' (DM to the bot) -> chat_id is the
        # sender's open_id. Carried so the card is pushed back with the matching
        # receive_id_type (chat_id vs open_id).
        chat_type = str(event.get("chat_type") or msg.get("chat_type") or "")
        content = msg.get("content") or ""
        if isinstance(content, str):
            try:
                content = json.loads(content)
            except Exception:
                content = {}
        text = ""
        if isinstance(content, dict):
            text = str(content.get("text") or "")
        if text.strip():
            from app.workers.device_provision_task import provision_from_feishu
            provision_from_feishu.apply_async(args=[text, chat_id, chat_type], queue="diagnosis")
            return {"handled": "provision_dispatched", "chat_id": chat_id, "chat_type": chat_type, "text": text[:80]}
        return {"handled": "empty_text"}

    value = action_value(payload)
    action = str(value.get("action") or "").upper()
    is_card_action = event_type in CARD_ACTION_EVENT_TYPES

    if action == "STOP_REPRODUCTION":
        session_id = str(value.get("session_id") or "")
        row = db.get(ReproductionSession, session_id)
        if not row:
            result = {"handled": "error", "reason": "REPRODUCTION_NOT_FOUND"}
            return _card_action_response(result) if is_card_action else result
        cancel_reproduction.apply_async(args=[row.id], queue="reproduction")
        result = {"handled": "stop_reproduction", "session_id": session_id}
        return _card_action_response(result) if is_card_action else result
    if action == "EXTERNAL_ACTION_COMPLETED":
        experiment_id = str(value.get("experiment_id") or "")
        exp = db.get(DiagnosticExperiment, experiment_id)
        if not exp:
            result = {"handled": "error", "reason": "EXPERIMENT_NOT_FOUND"}
            return _card_action_response(result) if is_card_action else result
        run = db.scalar(
            select(ExperimentRun)
            .where(ExperimentRun.experiment_id == experiment_id)
            .order_by(ExperimentRun.run_no.desc())
            .limit(1)
        )
        if not run:
            result = {"handled": "error", "reason": "EXPERIMENT_RUN_NOT_FOUND"}
            return _card_action_response(result) if is_card_action else result
        DiagnosticExperimentOrchestrator().complete_external_action(db, run=run, actor=actor)
        db.commit()
        result = {"handled": "external_action_completed", "experiment_id": experiment_id}
        return _card_action_response(result) if is_card_action else result
    if action == "OPEN_CASE":
        result = {"handled": "open_case"}
        return _card_action_response(result) if is_card_action else result
    return {"handled": "unhandled", "event_type": event_type, "action": action}
