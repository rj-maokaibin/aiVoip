from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_roles
from app.auth.providers import AuthIdentity
from app.contracts.enums import UserRole
from app.core.ids import new_id
from app.db.models import Case
from app.integrations.feishu.intake import route_intake
from app.integrations.feishu.semantic_router import shadow_semantic_route

router = APIRouter(tags=["ai-semantic"])
_admin = require_roles(UserRole.ADMIN, UserRole.SERVICE)


class SemanticResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=8000)
    attachments: list[dict] = Field(default_factory=list, max_length=32)
    message_id: str | None = Field(default=None, max_length=256)
    tenant_key: str | None = Field(default=None, max_length=128)
    chat_id: str | None = Field(default=None, max_length=256)


@router.post("/cases/{case_id}/ai/semantic/resolve")
def resolve_semantic_intent(
    case_id: str,
    req: SemanticResolveRequest,
    db: Session = Depends(get_db),
    _identity: AuthIdentity = Depends(_admin),
):
    """Run AI1 semantic extraction as a non-executing debug/shadow request."""
    case = db.get(Case, case_id)
    if case is None:
        raise HTTPException(404, "CASE_NOT_FOUND")
    deterministic = route_intake(text=req.text, attachments=req.attachments, has_thread_case=True)
    message_id = req.message_id or f"manual-ai1:{new_id()}"
    result = shadow_semantic_route(
        db,
        message_id=message_id,
        text=req.text,
        attachments=req.attachments,
        deterministic=deterministic,
        case_id=case.id,
        case_no=case.case_no,
        tenant_key=req.tenant_key,
        chat_id=req.chat_id,
        force=True,
    )
    if result is None:
        raise HTTPException(503, "AI_SEMANTIC_ROUTER_UNAVAILABLE")
    db.commit()
    return {
        "schema_version": "ai1-semantic-resolve-response-v1",
        "case_id": case.id,
        "case_no": case.case_no,
        "message_id": message_id,
        "status": result.status,
        "deterministic_intent": result.deterministic_intent,
        "final_intent": result.final_intent,
        "proposal": result.proposal,
        "record_id": result.record_id,
        "error_code": result.error_code,
        "execution_authority": "DETERMINISTIC_ROUTER_RBAC_POLICY",
        "semantic_proposal_is_non_executing": True,
    }
