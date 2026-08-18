from __future__ import annotations

import json
from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.errors import AppError
from app.core.config import settings
from app.integrations.feishu.authorized_events import dispatch_authorized_event
from app.integrations.feishu.events import CARD_ACTION_EVENT_TYPES, callback_actor
from app.integrations.feishu.transport import FeishuCallbackVerifier, FeishuTransportError

router = APIRouter(tags=["feishu-callback"])


@router.post("/integrations/feishu/callback")
async def feishu_callback(
    request: Request,
    db: Session = Depends(get_db),
    x_lark_signature: str | None = Header(default=None, alias="X-Lark-Signature"),
    x_lark_timestamp: str | None = Header(default=None, alias="X-Lark-Request-Timestamp"),
    x_lark_nonce: str | None = Header(default=None, alias="X-Lark-Request-Nonce"),
):
    if not settings.feishu_live_enabled:
        raise AppError("FEISHU_TRANSPORT_NOT_CONFIGURED")
    raw = await request.body()
    try:
        payload = json.loads(raw.decode("utf-8")) if raw else {}
    except Exception as exc:
        raise AppError("FEISHU_CALLBACK_INVALID") from exc
    try:
        FeishuCallbackVerifier().verify(
            timestamp=x_lark_timestamp,
            nonce=x_lark_nonce,
            signature=x_lark_signature,
            raw_body=raw,
            payload=payload,
        )
    except FeishuTransportError as exc:
        raise AppError("FEISHU_CALLBACK_INVALID", details={"reason": str(exc)}) from exc

    # URL verification callback used when registering a request URL.
    if payload.get("type") == "url_verification" and payload.get("challenge"):
        return {"challenge": payload["challenge"]}
    if "encrypt" in payload:
        # Security verification is supported; encrypted-body decryption is deliberately
        # not guessed without a separately frozen crypto contract/library.
        raise AppError("FEISHU_CALLBACK_INVALID", details={"reason": "ENCRYPTED_CALLBACK_BODY_NOT_SUPPORTED"})

    # Shared authorized dispatch: Identity/RBAC is evaluated before the existing
    # business handler whenever G2 is enabled. Webhook and WebSocket paths use the
    # same gateway, so control actions cannot bypass authorization by transport.
    header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
    event_type = str(header.get("event_type") or payload.get("type") or "")
    result = dispatch_authorized_event(db, payload=payload, actor=callback_actor(payload))
    if result.get("handled") == "error":
        raise AppError(result.get("reason", "FEISHU_CALLBACK_INVALID"), details=result)
    if event_type in CARD_ACTION_EVENT_TYPES:
        return {"code": 0, "msg": "ok", "toast": (result.get("toast") or {})}
    return {"code": 0, "msg": "ok"}