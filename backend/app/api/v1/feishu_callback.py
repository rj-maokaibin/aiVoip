from __future__ import annotations

import json
from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.errors import AppError
from app.core.config import settings
from app.db.models import DiagnosticExperiment, ExperimentRun, ReproductionSession
from app.experiments.orchestrator import DiagnosticExperimentOrchestrator
from app.integrations.feishu.transport import FeishuCallbackVerifier, FeishuTransportError
from app.workers.reproduction_tasks import cancel_reproduction

router = APIRouter(tags=["feishu-callback"])


def _action_value(payload: dict) -> dict:
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


def _callback_actor(payload: dict) -> str:
    for node in [payload.get("operator"), (payload.get("event") or {}).get("operator") if isinstance(payload.get("event"), dict) else None]:
        if isinstance(node, dict):
            for key in ("open_id", "user_id", "union_id"):
                if node.get(key):
                    return f"feishu:{node[key]}"
    return "feishu:callback"


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

    value = _action_value(payload)
    action = str(value.get("action") or "").upper()
    actor = _callback_actor(payload)

    # Text-message events (engineer @bot in a group or DM): provision a DUT for
    # background reproduction (open SSH + resolve Poseidon password). Text is
    # extracted from im.message.receive_v1 event.message.content (JSON string).
    header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
    event_type = str(header.get("event_type") or payload.get("type") or "")
    if event_type == "im.message.receive_v1":
        event = payload.get("event") or {}
        msg = event.get("message") or {}
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
            provision_from_feishu.apply_async(args=[text], queue="diagnosis")
            return {"code": 0, "msg": "ok"}
        return {"code": 0, "msg": "ok"}

    if action == "STOP_REPRODUCTION":
        session_id = str(value.get("session_id") or "")
        row = db.get(ReproductionSession, session_id)
        if not row:
            raise AppError("REPRODUCTION_NOT_FOUND")
        cancel_reproduction.apply_async(args=[row.id], queue="reproduction")
        return {"code": 0, "msg": "ok", "toast": {"type": "info", "content": "已请求安全停止自动复现"}}
    if action == "EXTERNAL_ACTION_COMPLETED":
        experiment_id = str(value.get("experiment_id") or "")
        exp = db.get(DiagnosticExperiment, experiment_id)
        if not exp:
            raise AppError("EXPERIMENT_NOT_FOUND")
        run = db.scalar(
            select(ExperimentRun)
            .where(ExperimentRun.experiment_id == experiment_id)
            .order_by(ExperimentRun.run_no.desc())
            .limit(1)
        )
        if not run:
            raise AppError("EXPERIMENT_RUN_NOT_FOUND")
        DiagnosticExperimentOrchestrator().complete_external_action(db, run=run, actor=actor)
        db.commit()
        return {"code": 0, "msg": "ok", "toast": {"type": "success", "content": "已记录现场操作完成"}}
    if action == "OPEN_CASE":
        return {"code": 0, "msg": "ok"}
    raise AppError("FEISHU_CALLBACK_INVALID", details={"reason": "UNKNOWN_ACTION", "action": action})
