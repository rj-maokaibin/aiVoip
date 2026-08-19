"""Feishu WebSocket long-connection event listener (official lark-oapi SDK).

Used when the deployment has NO public callback URL (company intranet / NAT):
instead of Feishu POSTing events to a callback, the backend opens an outbound
WebSocket to Feishu's long-connection endpoint and receives events over it.

The bootstrap handshake (POST /callback/ws/endpoint) and the WebSocket frame
protocol (protobuf-encoded, heartbeat/ping handled internally) are implemented
by the official lark-oapi SDK (lark.ws.Client). This module only wires the SDK's
event dispatcher to our shared authorized event gateway so a message/card action
has identical Identity/RBAC behavior over WebSocket and HTTP callback transports.

NOTE: lark.ws.Client.start() is BLOCKING and runs its own auto-reconnect loop,
so run_long_connection() starts the client on a daemon thread and returns
immediately with a LongConnectionHandle (the caller keeps the process alive).
"""
from __future__ import annotations

import logging
import threading

from app.core.config import settings
from app.integrations.feishu.authorized_events import dispatch_authorized_event
from app.integrations.feishu.events import callback_actor

log = logging.getLogger(__name__)


class FeishuLongConnectionError(RuntimeError):
    """Raised when the long connection cannot be started (config/credential)."""


def _sender_operator(sender_id) -> dict:
    """Map the SDK sender_id object to a minimal operator dict (empty if none)."""
    if sender_id is None:
        return {}
    for key in ("open_id", "user_id", "union_id"):
        value = getattr(sender_id, key, None)
        if value:
            return {key: value}
    return {}


def _message_payload(data) -> dict:
    """Normalize a SDK P2ImMessageReceiveV1 into the shared event payload."""
    event = getattr(data, "event", None)
    header = getattr(data, "header", None)
    message = getattr(event, "message", None) if event is not None else None
    chat_id = str(getattr(message, "chat_id", "") or "")
    chat_type = str(getattr(message, "chat_type", "") or "")
    content = str(getattr(message, "content", "") or "")
    message_type = str(getattr(message, "message_type", "") or "text")
    event_id = str(getattr(header, "event_id", "") or "")
    tenant_key = str(getattr(header, "tenant_key", "") or "")
    header_create_time = str(getattr(header, "create_time", "") or "")
    message_id = str(getattr(message, "message_id", "") or "")
    root_id = str(getattr(message, "root_id", "") or "")
    parent_id = str(getattr(message, "parent_id", "") or "")
    create_time = str(getattr(message, "create_time", "") or "")
    sender = getattr(event, "sender", None) if event is not None else None
    sender_id = getattr(sender, "sender_id", None) if sender is not None else None
    payload = {
        "header": {"event_type": "im.message.receive_v1", "event_id": event_id,
                   "tenant_key": tenant_key, "create_time": header_create_time},
        "event": {"chat_id": chat_id, "chat_type": chat_type,
                  "message": {"content": content, "chat_id": chat_id, "chat_type": chat_type,
                              "message_type": message_type,
                              "message_id": message_id, "root_id": root_id,
                              "parent_id": parent_id, "create_time": create_time}},
    }
    operator = _sender_operator(sender_id)
    if operator:
        payload["operator"] = operator
    return payload


def _dispatch(payload: dict) -> None:
    """Persist-and-dispatch an event payload in a dedicated DB session."""
    from app.db.session import SessionLocal
    actor = callback_actor(payload)
    with SessionLocal() as db:
        try:
            dispatch_authorized_event(db, payload=payload, actor=actor)
            db.commit()
        except Exception:
            log.exception("feishu long-connection event dispatch failed")
            db.rollback()


def _on_message_receive(data) -> None:
    """SDK handler for im.message.receive_v1 (runs on the SDK dispatcher thread)."""
    try:
        payload = _message_payload(data)
    except Exception:
        log.exception("feishu long-connection: failed to read message event")
        return
    _dispatch(payload)


def _card_action_payload(data) -> dict:
    """Normalize a SDK P2CardActionTrigger into the shared event payload.

    G2 requires tenant_key for `tenant_key + open_id` identity isolation. The
    previous adapter dropped it on the WebSocket card path, which would make card
    authorization weaker than message/webhook authorization.
    """
    event = getattr(data, "event", None)
    header = getattr(data, "header", None)
    action = getattr(event, "action", None) if event is not None else None
    operator = getattr(event, "operator", None) if event is not None else None
    context = getattr(event, "context", None) if event is not None else None
    value = getattr(action, "value", None) if action is not None else None
    payload = {
        "header": {
            "event_type": "card.action.trigger",
            "event_id": str(getattr(header, "event_id", "") or ""),
            "tenant_key": str(getattr(header, "tenant_key", "") or ""),
            "create_time": str(getattr(header, "create_time", "") or ""),
        },
        "event": {
            "action": {"value": value if isinstance(value, dict) else {}},
            "operator": {},
        },
        "operator": {},
    }
    if operator is not None:
        for key in ("open_id", "user_id", "union_id"):
            value_id = getattr(operator, key, None)
            if value_id:
                payload["event"]["operator"][key] = value_id
                payload["operator"][key] = value_id
                break
    if context is not None:
        chat_id = getattr(context, "open_chat_id", None)
        if chat_id:
            payload["event"]["chat_id"] = chat_id
    return payload


def _on_card_action(data):
    """SDK handler for card.action.trigger: RBAC dispatch + toast response."""
    from lark_oapi.event.callback.model.p2_card_action_trigger import (
        CallBackToast,
        P2CardActionTriggerResponse,
    )
    from app.db.session import SessionLocal
    try:
        payload = _card_action_payload(data)
    except Exception:
        log.exception("feishu long-connection: failed to read card action")
        return P2CardActionTriggerResponse()
    actor = callback_actor(payload)
    result = {}
    with SessionLocal() as db:
        try:
            result = dispatch_authorized_event(db, payload=payload, actor=actor)
            db.commit()
        except Exception:
            log.exception("feishu long-connection card action dispatch failed")
            db.rollback()
    toast = result.get("toast") or {}
    resp = P2CardActionTriggerResponse()
    if toast:
        t = CallBackToast()
        t.type = str(toast.get("type") or "info")
        t.content = str(toast.get("content") or "")
        resp.toast = t
    return resp


def build_event_handler():
    """Build the SDK EventDispatcherHandler wired to authorized dispatch."""
    import lark_oapi as lark
    return (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(_on_message_receive)
        .register_p2_card_action_trigger(_on_card_action)
        .build()
    )


class LongConnectionHandle:
    """Handle returned by run_long_connection; wraps the SDK client + thread."""

    def __init__(self, client, thread: threading.Thread):
        self.client = client
        self.thread = thread

    def is_alive(self) -> bool:
        return self.thread.is_alive()


def run_long_connection(*, log_level=None) -> LongConnectionHandle:
    """Start the official SDK long-connection listener on a daemon thread."""
    if not settings.feishu_live_enabled:
        raise FeishuLongConnectionError("FEISHU_LIVE_DISABLED")
    if not settings.feishu_app_id:
        raise FeishuLongConnectionError("FEISHU_APP_ID_NOT_CONFIGURED")
    if not settings.feishu_app_secret:
        raise FeishuLongConnectionError("FEISHU_APP_SECRET_NOT_CONFIGURED")

    import lark_oapi as lark
    event_handler = build_event_handler()
    client = lark.ws.Client(
        settings.feishu_app_id,
        settings.feishu_app_secret,
        event_handler=event_handler,
        log_level=log_level or lark.LogLevel.INFO,
        auto_reconnect=True,
    )
    thread = threading.Thread(target=client.start, name="feishu-long-connection", daemon=True)
    thread.start()
    log.info("feishu long-connection started on daemon thread (official SDK)")
    return LongConnectionHandle(client=client, thread=thread)