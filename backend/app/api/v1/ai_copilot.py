from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_permissions
from app.auth.providers import AuthIdentity
from app.contracts.enums import PermissionName
from app.core.config import settings
from app.core.ids import new_id
from app.db.models import Case
from app.copilot.service import CaseCopilotService
from app.services.audit import audit

router = APIRouter(tags=["ai-case-copilot"])
_reader = require_permissions(PermissionName.CASE_READ, PermissionName.REPORT_READ)


class CaseCopilotRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=8000)
    request_id: str | None = Field(default=None, min_length=1, max_length=192)


@router.post("/cases/{case_id}/ai/copilot")
def ask_case_copilot(
    case_id: str,
    req: CaseCopilotRequest,
    db: Session = Depends(get_db),
    identity: AuthIdentity = Depends(_reader),
):
    if not settings.ai_case_copilot_enabled:
        raise HTTPException(503, "AI_CASE_COPILOT_DISABLED")
    case = db.get(Case, case_id)
    if case is None:
        raise HTTPException(404, "CASE_NOT_FOUND")
    request_id = req.request_id or new_id()
    request_key = f"api:{case_id}:{request_id}"
    try:
        with db.begin_nested():
            result = CaseCopilotService().answer(
                db,
                case_id=case_id,
                question=req.question,
                request_key=request_key,
                actor_id=identity.actor_id,
                actor_role=identity.role,
            )
    except Exception as exc:
        error_code = type(exc).__name__[:128]
        audit(
            db,
            case_id=case_id,
            actor=identity.actor_id,
            event_type="AI_CASE_COPILOT_RUNTIME_FAILED",
            target_type="ai_case_copilot",
            target_id=None,
            detail={
                "schema_version": "ai-case-copilot-runtime-failure-v1",
                "error_code": error_code,
                "read_only": True,
                "parent_transaction_preserved": True,
                "source": "API",
            },
        )
        db.commit()
        raise HTTPException(503, "AI_CASE_COPILOT_RUNTIME_FAILED") from exc

    db.commit()
    safe_answer = result.answer
    if result.status == "GATEWAY_FAILED":
        safe_answer = "Case Copilot 当前不可用；确定性诊断与证据数据未受影响，请稍后重新查询。"
    elif result.status == "REJECTED":
        safe_answer = "本次 AI 回答未通过当前 Case 证据约束，因此未返回该回答。请查看确定性报告或补充证据。"
    return {
        "schema_version": "ai-case-copilot-response-v1",
        "case_id": case.id,
        "case_no": case.case_no,
        "request_id": request_id,
        "status": result.status,
        "answer": safe_answer,
        "proposal": result.proposal if result.status == "ANSWERED" else None,
        "grounding": result.grounding,
        "record_id": result.record_id,
        "routed_control_intent": result.routed_control_intent,
        "error_code": result.error_code,
        "read_only": True,
        "root_cause_authority": "DETERMINISTIC_OR_HUMAN_CONFIRMED_ONLY",
        "execution_authority": "DETERMINISTIC_ROUTER_RBAC_POLICY_ORCHESTRATOR",
    }
