"""Feishu WebSocket long-connection event listener (official lark-oapi SDK).

Used when the deployment has NO public callback URL (company intranet / NAT):
instead of Feishu POSTing events to a callback, the backend opens an outbound
WebSocket to Feishu's long-connection endpoint and receives events over it.

The bootstrap handshake (POST /callback/ws/endpoint) and the WebSocket frame
protocol (protobuf-encoded, heartbeat/ping handled internally) are implemented
by the official lark-oapi SDK (lark.ws.Client). This module only wires the SDK's
event dispatcher to our shared dispatch_event
(app.integrations.feishu.events), so a message handled over the long connection
provisions the DUT and binds the Case to the source chat exactly like the
webhook path.

NOTE: lark.ws.Client.start() is BLOCKING and runs its own auto-reconnect loop,
so run_long_connection() starts the client on a daemon thread and returns
immediately with a LongConnectionHandle (the caller keeps the process alive).
"""
from __future__ import annotations

import logging
import threading
from typing import Optional

from app.core.config import settings
from app.integrations.feishu.events import callback_actor, dispatch_event

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
    """Normalize a SDK P2ImMessageReceiveV1 into dispatch_event's payload shape.

    Accessor path mirrors the official samples:
        data.event.message.chat_id
        data.event.message.content   (JSON text, e.g. {"text": "..."})
        data.event.sender.sender_id  (open_id / user_id / union_id)
    """
    event = getattr(data, "event", None)
    message = getattr(event, "message", None) if event is not None else None
    chat_id = str(getattr(message, "chat_id", "") or "")
    content = str(getattr(message, "content", "") or "")
    sender = getattr(event, "sender", None) if event is not None else None
    sender_id = getattr(sender, "sender_id", None) if sender is not None else None
    payload = {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {"chat_id": chat_id, "message": {"content": content, "chat_id": chat_id}},
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
            dispatch_event(db, payload=payload, actor=actor)
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


def build_event_handler():
    """Build the SDK EventDispatcherHandler wired to our dispatch_event."""
    import lark_oapi as lark
    return (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(_on_message_receive)
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
    """Start the official SDK long-connection listener on a daemon thread.

    Raises FeishuLongConnectionError when the listener cannot start (live
    disabled or missing app id/secret). Returns immediately; the SDK keeps the
    connection alive (auto-reconnect) until the process exits.
    """
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
