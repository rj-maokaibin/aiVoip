from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.contracts.semantic_intent import SemanticIntentProposal, validate_semantic_proposal
from app.core.config import settings
from app.db.ai_intelligence_models import AISemanticIntentRecord
from app.integrations.feishu.intake import IntakeResult
from app.integrations.feishu.semantic_gateway import SemanticGatewayClient, SemanticGatewayError
from app.services.audit import audit


_CONTROL_INTENTS = {"STOP_REPRODUCTION", "EXTERNAL_ACTION_COMPLETED", "FIX_APPLIED"}
_COMPLEX_SIGNAL = re.compile(
    r"(?i)(换回|替换|更换|原装|得力|前后|相比|对比|比较|a/?b|又复现|再次复现|"
    r"环境|附件是新的|新(?:抓包|包|录音)|同时|然后|另外|并且|但是|而且)"
)


@dataclass(frozen=True)
class SemanticShadowResult:
    status: str
    final_intent: str
    deterministic_intent: str
    proposal: dict[str, Any] | None
    record_id: str | None
    error_code: str | None = None


def _input_hash(text: str, attachments: list[dict[str, Any]], case_id: str | None) -> str:
    normalized = json.dumps(
        {
            "text": text or "",
            "attachments": [
                {
                    "attachment_id": item.get("attachment_id") or item.get("file_key"),
                    "filename": item.get("filename"),
                    "message_type": item.get("message_type"),
                }
                for item in attachments[:32]
            ],
            "case_id": case_id,
        },
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def semantic_router_enabled() -> bool:
    return bool(getattr(settings, "ai_semantic_router_enabled", False))


def semantic_router_mode() -> str:
    return str(getattr(settings, "ai_semantic_router_mode", "OFF") or "OFF").upper()


def semantic_min_confidence() -> float:
    return float(getattr(settings, "ai_semantic_router_min_confidence", 0.80))


def needs_semantic_fallback(*, text: str, deterministic: IntakeResult) -> bool:
    """Conservative AI eligibility. Explicit controls never enter AI routing."""
    if deterministic.intent in _CONTROL_INTENTS:
        return False
    if deterministic.intent == "STATUS_QUERY" and deterministic.confidence >= 0.90:
        return False
    if _COMPLEX_SIGNAL.search(text or ""):
        return True
    if deterministic.intent in {"UNSUPPORTED", "CASE_FOLLOW_UP", "GENERAL_QUESTION"}:
        return True
    return deterministic.confidence < 0.90


def _record_existing(db: Session, message_id: str) -> AISemanticIntentRecord | None:
    return db.scalar(
        select(AISemanticIntentRecord).where(AISemanticIntentRecord.message_id == message_id).limit(1)
    )


def _allowed_case_refs(case_id: str | None, case_no: str | None) -> set[str]:
    return {str(x).strip().upper() for x in (case_id, case_no) if x}


def _validate_case_authority(
    proposal: SemanticIntentProposal,
    *,
    case_id: str | None,
    case_no: str | None,
) -> None:
    if not proposal.case_ref:
        return
    allowed = _allowed_case_refs(case_id, case_no)
    if proposal.case_ref.strip().upper() not in allowed:
        raise ValueError("SEMANTIC_CASE_OVERRIDE_FORBIDDEN")


def _result_from_record(row: AISemanticIntentRecord) -> SemanticShadowResult:
    return SemanticShadowResult(
        status=row.status,
        final_intent=row.deterministic_intent,
        deterministic_intent=row.deterministic_intent,
        proposal=row.proposal_json or None,
        record_id=row.id,
        error_code=row.error_code,
    )


def shadow_semantic_route(
    db: Session,
    *,
    message_id: str,
    text: str,
    attachments: list[dict[str, Any]],
    deterministic: IntakeResult,
    case_id: str | None,
    case_no: str | None = None,
    tenant_key: str | None = None,
    chat_id: str | None = None,
    gateway: SemanticGatewayClient | None = None,
    force: bool = False,
) -> SemanticShadowResult | None:
    """Run AI1 in non-authoritative SHADOW mode.

    The returned ``final_intent`` is always the deterministic intent in V1.
    A model proposal is observation/evaluation data only and cannot execute a
    workflow, change Case authority, or bypass G2 authorization.
    """
    existing = _record_existing(db, message_id)
    if existing is not None:
        return _result_from_record(existing)
    if not force and (not semantic_router_enabled() or semantic_router_mode() != "SHADOW"):
        return None

    eligible = needs_semantic_fallback(text=text, deterministic=deterministic)
    base = dict(
        case_id=case_id,
        tenant_key=tenant_key,
        chat_id=chat_id,
        message_id=message_id,
        input_hash=_input_hash(text, attachments, case_id),
        deterministic_intent=deterministic.intent,
        deterministic_confidence=float(deterministic.confidence),
        proposal_json={},
        validated_intent=None,
        ai_confidence=None,
        prompt_version="feishu-semantic-router-v1",
        model_name=None,
    )
    if not eligible:
        row = AISemanticIntentRecord(status="BYPASSED", error_code=None, **base)
        db.add(row)
        db.flush()
        audit(
            db,
            case_id=case_id,
            actor="ai1-semantic-router",
            event_type="AI_SEMANTIC_ROUTER_BYPASSED",
            target_type="feishu_message",
            target_id=message_id,
            detail={"schema_version": "ai1-semantic-audit-v1", "deterministic_intent": deterministic.intent},
        )
        return _result_from_record(row)

    gateway = gateway or SemanticGatewayClient()
    context = {
        "resolved_case": {"case_id": case_id, "case_no": case_no},
        "thread_case_resolved": bool(case_id),
        "case_authority": "G1_CASE_RESOLVER",
    }
    try:
        raw = gateway.resolve(
            text=text,
            attachments=attachments,
            deterministic=deterministic.to_dict(),
            context=context,
        )
        proposal = validate_semantic_proposal(raw["proposal"])
        _validate_case_authority(proposal, case_id=case_id, case_no=case_no)
        if proposal.confidence < semantic_min_confidence():
            raise ValueError("SEMANTIC_CONFIDENCE_BELOW_THRESHOLD")
        row = AISemanticIntentRecord(
            status="SHADOW_VALID",
            proposal_json=proposal.model_dump(mode="json"),
            validated_intent=proposal.intent,
            ai_confidence=proposal.confidence,
            prompt_version=str(raw.get("prompt_version") or "feishu-semantic-router-v1")[:64],
            model_name=str(raw.get("model") or "")[:128] or None,
            error_code=None,
            **{k: v for k, v in base.items() if k not in {"proposal_json", "validated_intent", "ai_confidence", "prompt_version", "model_name"}},
        )
        db.add(row)
        db.flush()
        audit(
            db,
            case_id=case_id,
            actor="ai1-semantic-router",
            event_type="AI_SEMANTIC_PROPOSAL_RECORDED",
            target_type="ai_semantic_intent",
            target_id=row.id,
            detail={
                "schema_version": "ai1-semantic-audit-v1",
                "deterministic_intent": deterministic.intent,
                "semantic_intent": proposal.intent,
                "confidence": proposal.confidence,
                "shadow_only": True,
            },
        )
        return _result_from_record(row)
    except SemanticGatewayError as exc:
        code = str(exc).split(":", 1)[0][:128]
        status = "GATEWAY_FAILED"
    except (ValidationError, ValueError, KeyError, TypeError) as exc:
        code = str(exc).split("\n", 1)[0][:128]
        status = "REJECTED"

    row = AISemanticIntentRecord(status=status, error_code=code, **base)
    db.add(row)
    db.flush()
    audit(
        db,
        case_id=case_id,
        actor="ai1-semantic-router",
        event_type="AI_SEMANTIC_PROPOSAL_REJECTED",
        target_type="ai_semantic_intent",
        target_id=row.id,
        detail={
            "schema_version": "ai1-semantic-audit-v1",
            "error_code": code,
            "shadow_only": True,
            "deterministic_intent": deterministic.intent,
        },
    )
    return _result_from_record(row)
