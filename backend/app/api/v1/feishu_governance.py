from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_roles
from app.api.feishu_permissions import FeishuCapability
from app.auth.providers import AuthIdentity
from app.contracts.enums import UserRole
from app.db.feishu_governance_models import CaseAclEntry, FeishuUserIdentity
from app.db.models import Case, FeishuCaseBinding
from app.integrations.feishu.case_resolver import close_binding_lifecycle
from app.integrations.feishu.service import bind_case_to_chat
from app.services.audit import audit
from app.services.idempotency import begin_idempotent, complete_idempotent

router = APIRouter(tags=["feishu-governance"])
_admin = require_roles(UserRole.ADMIN, UserRole.SERVICE)


class IdentityUpsertRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_key: str = Field(min_length=1, max_length=128)
    open_id: str = Field(min_length=1, max_length=128)
    internal_actor_id: str = Field(min_length=1, max_length=128)
    role: UserRole
    status: str = Field(default="ACTIVE", pattern="^(ACTIVE|DISABLED|PENDING_MAPPING)$")
    union_id: str | None = Field(default=None, max_length=128)
    user_id: str | None = Field(default=None, max_length=128)
    display_name: str | None = Field(default=None, max_length=256)
    metadata: dict = Field(default_factory=dict)


class IdentityPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    internal_actor_id: str | None = Field(default=None, min_length=1, max_length=128)
    role: UserRole | None = None
    status: str | None = Field(default=None, pattern="^(ACTIVE|DISABLED|PENDING_MAPPING)$")
    union_id: str | None = Field(default=None, max_length=128)
    user_id: str | None = Field(default=None, max_length=128)
    display_name: str | None = Field(default=None, max_length=256)
    metadata: dict | None = None


class CaseAclItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actor_id: str = Field(min_length=1, max_length=128)
    capability: FeishuCapability
    effect: str = Field(pattern="^(ALLOW|DENY)$")
    expires_at: datetime | None = None


class ReplaceCaseAclRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entries: list[CaseAclItem] = Field(default_factory=list, max_length=500)


class BindCaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: str = Field(min_length=1, max_length=36)
    tenant_key: str = Field(min_length=1, max_length=128)
    chat_id: str = Field(min_length=1, max_length=256)
    chat_type: str = Field(default="group", max_length=32)


def _identity_out(row: FeishuUserIdentity) -> dict:
    return {
        "id": row.id,
        "tenant_key": row.tenant_key,
        "open_id": row.open_id,
        "union_id": row.union_id,
        "user_id": row.user_id,
        "internal_actor_id": row.internal_actor_id,
        "role": row.role,
        "status": row.status,
        "display_name": row.display_name,
        "metadata": row.metadata_json or {},
        "last_seen_at": row.last_seen_at,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _complete(db: Session, handle, response: dict | list, *, resource_type: str, resource_id: str | None = None):
    complete_idempotent(
        db, handle, response=response, status_code=200,
        resource_type=resource_type, resource_id=resource_id,
    )
    db.commit()
    return response


@router.get("/feishu/identities")
def list_identities(
    tenant_key: str | None = None,
    status: str | None = None,
    db: Session = Depends(get_db),
    _identity: AuthIdentity = Depends(_admin),
):
    query = select(FeishuUserIdentity)
    if tenant_key:
        query = query.where(FeishuUserIdentity.tenant_key == tenant_key)
    if status:
        query = query.where(FeishuUserIdentity.status == status.upper())
    rows = list(db.scalars(query.order_by(FeishuUserIdentity.updated_at.desc()).limit(1000)))
    return [_identity_out(row) for row in rows]


@router.post("/feishu/identities")
def upsert_identity(
    req: IdentityUpsertRequest,
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    identity: AuthIdentity = Depends(_admin),
):
    handle = begin_idempotent(
        db, scope="POST:/api/v1/feishu/identities", key=idempotency_key,
        payload=req.model_dump(mode="json"),
    )
    if handle.replay is not None:
        return handle.replay
    row = db.scalar(select(FeishuUserIdentity).where(
        FeishuUserIdentity.tenant_key == req.tenant_key,
        FeishuUserIdentity.open_id == req.open_id,
    ).limit(1))
    before = _identity_out(row) if row else None
    if row is None:
        row = FeishuUserIdentity(
            tenant_key=req.tenant_key,
            open_id=req.open_id,
            internal_actor_id=req.internal_actor_id,
            role=req.role.value,
            status=req.status,
        )
        db.add(row)
    row.internal_actor_id = req.internal_actor_id
    row.role = req.role.value
    row.status = req.status
    row.union_id = req.union_id
    row.user_id = req.user_id
    row.display_name = req.display_name
    row.metadata_json = dict(req.metadata)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "FEISHU_IDENTITY_CONFLICT") from exc
    response = _identity_out(row)
    audit(
        db, actor=identity.actor_id, event_type="FEISHU_IDENTITY_UPDATED",
        target_type="feishu_user_identity", target_id=row.id,
        before=before, after=response,
        detail={"schema_version": "feishu-identity-management-v1"},
    )
    return _complete(db, handle, response, resource_type="feishu_user_identity", resource_id=row.id)


@router.patch("/feishu/identities/{identity_id}")
def patch_identity(
    identity_id: str,
    req: IdentityPatchRequest,
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    identity: AuthIdentity = Depends(_admin),
):
    row = db.get(FeishuUserIdentity, identity_id)
    if row is None:
        raise HTTPException(404, "FEISHU_IDENTITY_NOT_FOUND")
    payload = req.model_dump(mode="json", exclude_unset=True)
    handle = begin_idempotent(
        db, scope=f"PATCH:/api/v1/feishu/identities/{identity_id}",
        key=idempotency_key, payload=payload,
    )
    if handle.replay is not None:
        return handle.replay
    before = _identity_out(row)
    if req.internal_actor_id is not None:
        row.internal_actor_id = req.internal_actor_id
    if req.role is not None:
        row.role = req.role.value
    if req.status is not None:
        row.status = req.status
    if "union_id" in req.model_fields_set:
        row.union_id = req.union_id
    if "user_id" in req.model_fields_set:
        row.user_id = req.user_id
    if "display_name" in req.model_fields_set:
        row.display_name = req.display_name
    if req.metadata is not None:
        row.metadata_json = dict(req.metadata)
    db.flush()
    response = _identity_out(row)
    audit(
        db, actor=identity.actor_id, event_type="FEISHU_IDENTITY_UPDATED",
        target_type="feishu_user_identity", target_id=row.id,
        before=before, after=response,
        detail={"schema_version": "feishu-identity-management-v1"},
    )
    return _complete(db, handle, response, resource_type="feishu_user_identity", resource_id=row.id)


@router.get("/cases/{case_id}/acl")
def get_case_acl(
    case_id: str,
    db: Session = Depends(get_db),
    _identity: AuthIdentity = Depends(_admin),
):
    if db.get(Case, case_id) is None:
        raise HTTPException(404, "CASE_NOT_FOUND")
    rows = list(db.scalars(select(CaseAclEntry).where(
        CaseAclEntry.case_id == case_id,
    ).order_by(CaseAclEntry.actor_id.asc(), CaseAclEntry.capability.asc())))
    return [{
        "id": row.id,
        "case_id": row.case_id,
        "actor_id": row.actor_id,
        "capability": row.capability,
        "effect": row.effect,
        "expires_at": row.expires_at,
        "created_by": row.created_by,
    } for row in rows]


@router.put("/cases/{case_id}/acl")
def replace_case_acl(
    case_id: str,
    req: ReplaceCaseAclRequest,
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    identity: AuthIdentity = Depends(_admin),
):
    if db.get(Case, case_id) is None:
        raise HTTPException(404, "CASE_NOT_FOUND")
    normalized = sorted(
        [item.model_dump(mode="json") for item in req.entries],
        key=lambda row: (row["actor_id"], row["capability"], row["effect"]),
    )
    handle = begin_idempotent(
        db, scope=f"PUT:/api/v1/cases/{case_id}/acl", key=idempotency_key,
        payload={"entries": normalized},
    )
    if handle.replay is not None:
        return handle.replay
    existing = list(db.scalars(select(CaseAclEntry).where(CaseAclEntry.case_id == case_id)))
    before = [{"actor_id": row.actor_id, "capability": row.capability, "effect": row.effect} for row in existing]
    for row in existing:
        db.delete(row)
    seen: set[tuple[str, str]] = set()
    for item in req.entries:
        key = (item.actor_id, item.capability.value)
        if key in seen:
            db.rollback()
            raise HTTPException(422, "DUPLICATE_CASE_ACL_CAPABILITY")
        seen.add(key)
        db.add(CaseAclEntry(
            case_id=case_id,
            actor_id=item.actor_id,
            capability=item.capability.value,
            effect=item.effect,
            expires_at=item.expires_at,
            created_by=identity.actor_id,
        ))
    db.flush()
    response = {"case_id": case_id, "entries": normalized}
    audit(
        db, case_id=case_id, actor=identity.actor_id,
        event_type="CASE_ACL_REPLACED", target_type="case_acl", target_id=case_id,
        before={"entries": before}, after=response,
        detail={"schema_version": "feishu-case-acl-v1"},
    )
    return _complete(db, handle, response, resource_type="case_acl", resource_id=case_id)


@router.get("/feishu/cases/bindings")
def list_bindings(
    tenant_key: str | None = None,
    chat_id: str | None = None,
    db: Session = Depends(get_db),
    _identity: AuthIdentity = Depends(_admin),
):
    filters = []
    params: dict[str, str] = {}
    if tenant_key is not None:
        filters.append("source_tenant_key = :tenant_key")
        params["tenant_key"] = tenant_key
    if chat_id is not None:
        filters.append("receive_id = :chat_id")
        params["chat_id"] = chat_id
    where = (" WHERE " + " AND ".join(filters)) if filters else ""
    rows = db.execute(text(
        "SELECT id, case_id, source_tenant_key, receive_id, receive_id_type, "
        "binding_state, binding_generation, activated_at, closed_at, created_by_open_id, close_reason "
        f"FROM feishu_case_bindings{where} ORDER BY created_at DESC LIMIT 1000"
    ), params).mappings().all()
    return [dict(row) for row in rows]


@router.post("/feishu/cases/bindings")
def bind_case(
    req: BindCaseRequest,
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    identity: AuthIdentity = Depends(_admin),
):
    if db.get(Case, req.case_id) is None:
        raise HTTPException(404, "CASE_NOT_FOUND")
    handle = begin_idempotent(
        db, scope="POST:/api/v1/feishu/cases/bindings", key=idempotency_key,
        payload=req.model_dump(mode="json"),
    )
    if handle.replay is not None:
        return handle.replay
    binding = bind_case_to_chat(
        db, case_id=req.case_id, chat_id=req.chat_id, chat_type=req.chat_type,
        source_context={
            "tenant_key": req.tenant_key,
            "sender_open_id": identity.actor_id,
            "normalized_text": "ADMIN_BIND",
        },
    )
    response = {"binding_id": binding.id if binding else None, **req.model_dump(mode="json")}
    audit(
        db, case_id=req.case_id, actor=identity.actor_id,
        event_type="FEISHU_CASE_BINDING_MANAGED", target_type="feishu_case_binding",
        target_id=binding.id if binding else None,
        after=response,
        detail={"schema_version": "feishu-case-binding-management-v1", "operation": "BIND"},
    )
    return _complete(db, handle, response, resource_type="feishu_case_binding", resource_id=binding.id if binding else None)


@router.post("/feishu/cases/{case_id}/close-binding")
def close_case_binding(
    case_id: str,
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    identity: AuthIdentity = Depends(_admin),
):
    handle = begin_idempotent(
        db, scope=f"POST:/api/v1/feishu/cases/{case_id}/close-binding",
        key=idempotency_key, payload={"case_id": case_id},
    )
    if handle.replay is not None:
        return handle.replay
    binding = db.scalar(select(FeishuCaseBinding).where(
        FeishuCaseBinding.case_id == case_id,
        FeishuCaseBinding.receive_id_type == "chat_id",
    ).order_by(FeishuCaseBinding.created_at.desc()).limit(1))
    if binding is None:
        db.rollback()
        raise HTTPException(404, "FEISHU_CASE_BINDING_NOT_FOUND")
    close_binding_lifecycle(db, binding_id=binding.id, reason="ADMIN_CLOSED")
    response = {"case_id": case_id, "binding_id": binding.id, "status": "CLOSED"}
    audit(
        db, case_id=case_id, actor=identity.actor_id,
        event_type="FEISHU_CASE_BINDING_MANAGED", target_type="feishu_case_binding",
        target_id=binding.id, after=response,
        detail={"schema_version": "feishu-case-binding-management-v1", "operation": "CLOSE"},
    )
    return _complete(db, handle, response, resource_type="feishu_case_binding", resource_id=binding.id)
