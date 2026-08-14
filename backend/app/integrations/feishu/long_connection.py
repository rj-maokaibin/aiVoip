"""Feishu WebSocket long-connection event listener.

Used when the deployment has NO public callback URL (company intranet / NAT):
instead of Feishu POSTing events to a callback, the backend opens an outbound
WebSocket to Feishu's long-connection endpoint and receives events over it.

Flow (Feishu open platform, long-connection mode):
  1. GET {feishu_base_url}/event/v1/websocket with Authorization: Bearer
     <tenant_access_token> -> {"data": {"endpoint": "wss://...", "token": "..."}}
  2. Open the WebSocket; the server sends frames:
       - {"type": "challenge", "data": {"challenge": "..."}}  -> reply challenge
       - {"type": "ping", "data": {...}}                       -> reply pong
       - {"type": "event", "data": {header, event}}            -> dispatch_event
  3. Auto-reconnect with backoff; re-fetch endpoint/token on reconnect.

This module is transport-only: event handling is delegated to
app.integrations.feishu.events.dispatch_event (shared with the HTTP callback), so
a message handled over the long connection provisions the DUT and binds the Case
to the source chat exactly like the webhook path.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Awaitable, Callable

import httpx
import websockets

from app.core.config import settings
from app.integrations.feishu.events import callback_actor, dispatch_event
from app.integrations.feishu.transport import FeishuLiveTransport

log = logging.getLogger(__name__)

# Frame types defined by Feishu's long-connection protocol.
CHALLENGE = "challenge"
PING = "ping"
PONG = "pong"
EVENT = "event"


class FeishuLongConnectionError(RuntimeError):
    pass


async def fetch_websocket_endpoint(transport: FeishuLiveTransport) -> tuple[str, str]:
    """Return (endpoint, token) from Feishu's long-connection bootstrap API."""
    if not settings.feishu_live_enabled:
        raise FeishuLongConnectionError("FEISHU_LIVE_DISABLED")
    if not settings.feishu_app_id:
        raise FeishuLongConnectionError("FEISHU_APP_ID_NOT_CONFIGURED")
    token = await transport._tenant_token()
    url = settings.feishu_base_url.rstrip("/") + "/event/v1/websocket"
    async with httpx.AsyncClient(timeout=settings.feishu_timeout_seconds) as client:
        response = await client.get(url, headers={"Authorization": f"Bearer {token}"})
    try:
        data = response.json()
    except Exception as exc:
        raise FeishuLongConnectionError(f"FEISHU_WS_INVALID_RESPONSE:{response.status_code}") from exc
    if response.status_code >= 400 or int(data.get("code", 0)) != 0:
        raise FeishuLongConnectionError(f"FEISHU_WS_BOOTSTRAP_FAILED:{data.get('code', response.status_code)}")
    body = data.get("data") or {}
    endpoint = str(body.get("endpoint") or "")
    ws_token = str(body.get("token") or "")
    if not endpoint:
        raise FeishuLongConnectionError("FEISHU_WS_ENDPOINT_MISSING")
    return endpoint, ws_token


async def _handle_frame(ws, frame: dict, *, on_event: Callable[[dict], Awaitable[None]]) -> str:
    """Process one frame; reply to challenge/ping. Returns a short tag."""
    frame_type = str(frame.get("type") or "")
    if frame_type == CHALLENGE:
        await ws.send(json.dumps({"type": CHALLENGE, "data": frame.get("data")}, ensure_ascii=False))
        return "challenge"
    if frame_type == PING:
        await ws.send(json.dumps({"type": PONG, "data": frame.get("data")}, ensure_ascii=False))
        return "pong"
    if frame_type == EVENT:
        data = frame.get("data") or {}
        await on_event(data)
        return "event"
    return f"unknown:{frame_type}"


async def _on_event(data: dict) -> None:
    """Persist-and-dispatch an event frame's data in a dedicated DB session."""
    from app.db.session import SessionLocal
    payload = data if isinstance(data, dict) else {}
    actor = callback_actor(payload)
    with SessionLocal() as db:
        try:
            dispatch_event(db, payload=payload, actor=actor)
            db.commit()
        except Exception:
            log.exception("feishu long-connection event dispatch failed")
            db.rollback()


async def run_long_connection(*, on_event: Callable[[dict], Awaitable[None]] | None = None,
                              max_retries: int = -1, backoff_seconds: float = 5.0) -> int:
    """Run the long-connection listener until stopped or max_retries exhausted.

    on_event defaults to _on_event. Returns the number of reconnects performed
    (useful for tests / diagnostics).
    """
    on_event = on_event or _on_event
    transport = FeishuLiveTransport()
    attempts = 0
    reconnects = 0
    while max_retries < 0 or attempts < max_retries:
        attempts += 1
        try:
            endpoint, ws_token = await fetch_websocket_endpoint(transport)
            log.info("feishu long-connection connecting: %s", endpoint)
            # The per-connection token is passed as a query param by the official
            # client; Feishu's wss endpoint validates it on connect.
            sep = "&" if "?" in endpoint else "?"
            url = f"{endpoint}{sep}token={ws_token}"
            async with websockets.connect(url, max_size=4 * 1024 * 1024) as ws:
                while True:
                    raw = await ws.recv()
                    try:
                        frame = json.loads(raw)
                    except Exception:
                        log.warning("feishu ws non-json frame dropped: %.120s", raw)
                        continue
                    tag = await _handle_frame(ws, frame, on_event=on_event)
                    log.debug("feishu ws frame handled: %s", tag)
        except Exception as exc:
            log.warning("feishu long-connection attempt %d failed: %s", attempts, type(exc).__name__)
        if max_retries >= 0 and attempts >= max_retries:
            break
        reconnects += 1
        await asyncio.sleep(backoff_seconds)
    return reconnects
