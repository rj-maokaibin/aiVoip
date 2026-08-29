#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

CONTRACT = "conversation-feishu-live-acceptance-v1"
PREFLIGHT_CONTRACT = "voip-live-acceptance-preflight-v2"
CONFIRMATION = "REPLY_TO_DEDICATED_FEISHU_ACCEPTANCE_MESSAGE"


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def current_source_revision() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL
    ).strip()


def load_and_validate_preflight(path: Path, *, expected_revision: str) -> dict[str, Any]:
    if not path.is_file():
        raise RuntimeError("CONVERSATION_LIVE_PREFLIGHT_MISSING")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("contract") != PREFLIGHT_CONTRACT:
        raise RuntimeError("CONVERSATION_LIVE_PREFLIGHT_CONTRACT_MISMATCH")
    if payload.get("status") != "PASS" or payload.get("mutation_allowed") is not True:
        raise RuntimeError("CONVERSATION_LIVE_PREFLIGHT_NOT_MUTATION_READY")
    observed_revision = str(payload.get("source_revision") or "")
    if observed_revision != expected_revision:
        raise RuntimeError("CONVERSATION_LIVE_PREFLIGHT_REVISION_MISMATCH")
    return payload


def validate_live_target(*, message_id: str, confirmation: str) -> str:
    target = str(message_id or "").strip()
    if not target or not target.startswith("om_") or len(target) < 12:
        raise RuntimeError("CONVERSATION_LIVE_DEDICATED_MESSAGE_ID_REQUIRED")
    if str(confirmation or "").strip() != CONFIRMATION:
        raise RuntimeError("CONVERSATION_LIVE_EXPLICIT_CONFIRMATION_REQUIRED")
    return target


def execute_live_reply(*, message_id: str, text: str) -> dict[str, Any]:
    """Execute exactly one production reply and verify its persisted delivery trace.

    This helper intentionally calls the production Celery task synchronously via
    ``Task.apply``.  It therefore exercises the same Feishu transport, retry/trace
    code and database models without publishing a second asynchronous task that
    could outlive the acceptance process.
    """
    from sqlalchemy import select

    from app.conversation.delivery import semantic_reply_key
    from app.core.config import settings
    from app.db.conversation_models import FeishuReplyDeliveryTrace
    from app.db.session import SessionLocal
    from app.workers.device_provision_task import reply_feishu_text

    if str(settings.app_env or "").lower() != "production":
        raise RuntimeError("CONVERSATION_LIVE_PRODUCTION_ENV_REQUIRED")
    if settings.feishu_live_enabled is not True:
        raise RuntimeError("CONVERSATION_LIVE_FEISHU_DISABLED")
    if settings.conversation_cycle_decoupled is not True:
        raise RuntimeError("CONVERSATION_LIVE_CYCLE_DECOUPLING_REQUIRED")
    if settings.feishu_reply_retry_enabled is not True:
        raise RuntimeError("CONVERSATION_LIVE_REPLY_RETRY_REQUIRED")

    result = reply_feishu_text.apply(args=[message_id, text], throw=True).get(propagate=True)
    if not isinstance(result, dict) or result.get("status") != "SENT":
        raise RuntimeError("CONVERSATION_LIVE_REPLY_NOT_SENT")

    key = semantic_reply_key(message_id, text)
    db = SessionLocal()
    try:
        trace = db.scalar(
            select(FeishuReplyDeliveryTrace)
            .where(
                FeishuReplyDeliveryTrace.source_message_id == message_id,
                FeishuReplyDeliveryTrace.semantic_key == key,
            )
            .order_by(FeishuReplyDeliveryTrace.created_at.desc())
            .limit(1)
        )
        if trace is None:
            raise RuntimeError("CONVERSATION_LIVE_REPLY_TRACE_MISSING")
        if trace.stage != "SENT":
            raise RuntimeError(f"CONVERSATION_LIVE_REPLY_TRACE_NOT_SENT:{trace.stage}")
        if int(trace.attempt_count or 0) < 1:
            raise RuntimeError("CONVERSATION_LIVE_REPLY_TRACE_ATTEMPT_MISSING")
        if not trace.sent_message_id:
            raise RuntimeError("CONVERSATION_LIVE_SENT_MESSAGE_ID_MISSING")
        return {
            "status": "PASS",
            "contract": CONTRACT,
            "source_message_sha256": _sha256_text(message_id),
            "sent_message_sha256": _sha256_text(str(trace.sent_message_id)),
            "semantic_key": key,
            "delivery_stage": trace.stage,
            "attempt_count": int(trace.attempt_count or 0),
            "trace_id": trace.id,
            "case_bound": bool(trace.case_id),
        }
    finally:
        db.close()


def run(*, preflight_path: Path, message_id: str, confirmation: str, text: str) -> dict[str, Any]:
    revision = current_source_revision()
    preflight = load_and_validate_preflight(preflight_path, expected_revision=revision)
    target = validate_live_target(message_id=message_id, confirmation=confirmation)
    clean_text = str(text or "").strip()
    if not clean_text:
        raise RuntimeError("CONVERSATION_LIVE_REPLY_TEXT_REQUIRED")
    if len(clean_text) > 500:
        raise RuntimeError("CONVERSATION_LIVE_REPLY_TEXT_TOO_LONG")

    result = execute_live_reply(message_id=target, text=clean_text)
    result.update(
        {
            "source_revision": revision,
            "preflight_contract": preflight.get("contract"),
            "preflight_runtime_fingerprint": preflight.get("runtime_fingerprint"),
            "mutation_scope": "ONE_DEDICATED_FEISHU_MESSAGE_REPLY",
            "diagnostic_authority_changed": False,
            "device_action_executed": False,
        }
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Explicit real-tenant Feishu reply acceptance for Conversation Platform V1"
    )
    parser.add_argument("--preflight-result", type=Path, required=True)
    parser.add_argument("--message-id", required=True)
    parser.add_argument("--confirmation", required=True)
    parser.add_argument(
        "--text",
        default="VOIP AI Conversation Live Acceptance：真实飞书回复链路验证。无需回复此消息。",
    )
    parser.add_argument(
        "--result", type=Path, default=Path("validation/conversation_feishu_live_acceptance_v1.json")
    )
    args = parser.parse_args()

    try:
        payload = run(
            preflight_path=args.preflight_result,
            message_id=args.message_id,
            confirmation=args.confirmation,
            text=args.text,
        )
    except Exception as exc:
        payload = {
            "status": "FAIL",
            "contract": CONTRACT,
            "error_code": type(exc).__name__,
            "error_message": str(exc)[:300],
        }

    args.result.parent.mkdir(parents=True, exist_ok=True)
    args.result.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if payload.get("status") == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
