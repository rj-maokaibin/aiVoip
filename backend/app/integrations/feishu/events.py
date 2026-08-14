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
            provision_from_feishu.apply_async(args=[text, chat_id], queue="diagnosis")
            return {"handled": "provision_dispatched", "chat_id": chat_id, "text": text[:80]}
        return {"handled": "empty_text"}

    value = action_value(payload)
    action = str(value.get("action") or "").upper()

    if action == "STOP_REPRODUCTION":
        session_id = str(value.get("session_id") or "")
        row = db.get(ReproductionSession, session_id)
        if not row:
            return {"handled": "error", "reason": "REPRODUCTION_NOT_FOUND"}
        cancel_reproduction.apply_async(args=[row.id], queue="reproduction")
        return {"handled": "stop_reproduction", "session_id": session_id}
    if action == "EXTERNAL_ACTION_COMPLETED":
        experiment_id = str(value.get("experiment_id") or "")
        exp = db.get(DiagnosticExperiment, experiment_id)
        if not exp:
            return {"handled": "error", "reason": "EXPERIMENT_NOT_FOUND"}
        run = db.scalar(
            select(ExperimentRun)
            .where(ExperimentRun.experiment_id == experiment_id)
            .order_by(ExperimentRun.run_no.desc())
            .limit(1)
        )
        if not run:
            return {"handled": "error", "reason": "EXPERIMENT_RUN_NOT_FOUND"}
        DiagnosticExperimentOrchestrator().complete_external_action(db, run=run, actor=actor)
        db.commit()
        return {"handled": "external_action_completed", "experiment_id": experiment_id}
    if action == "OPEN_CASE":
        return {"handled": "open_case"}
    return {"handled": "unhandled", "event_type": event_type, "action": action}
