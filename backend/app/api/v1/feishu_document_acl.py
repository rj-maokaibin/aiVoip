from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_roles
from app.api.feishu_permissions import FeishuCapability
from app.auth.providers import AuthIdentity
from app.contracts.enums import UserRole
from app.db.evidence_report_models import FeishuEvidenceDocumentBinding
from app.db.feishu_governance_models import FeishuDocumentAclBinding
from app.db.models import Case
from app.services.audit import audit
from app.services.idempotency import begin_idempotent, complete_idempotent
from app.workers.feishu_document_acl_task import sync_document_acl

router = APIRouter(tags=["feishu-document-acl"])
_admin = require_roles(UserRole.ADMIN, UserRole.SERVICE)


class ManualAclSyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    force_revision: bool = False


def _iso(value):
    return value.isoformat() if value is not None else None


def _out(row: FeishuDocumentAclBinding | None, *, case_id: str, document_id: str | None) -> dict:
    if row is None:
        return {
            "case_id": case_id,
            "document_id": document_id,
            "status": "NOT_CONFIGURED" if document_id else "DOCUMENT_NOT_BOUND",
            "capability": FeishuCapability.MANAGE_DOCUMENT_ACL.value,
        }
    return {
        "id": row.id,
        "case_id": row.case_id,
        "document_id": row.document_id,
        "tenant_key": row.tenant_key,
        "chat_id": row.chat_id,
        "sync_mode": row.sync_mode,
        "effective_mode": row.effective_mode,
        "desired_permission": row.desired_permission,
        "desired_revision": row.desired_revision,
        "applied_revision": row.applied_revision,
        "status": row.status,
        "retry_count": row.retry_count,
        "last_synced_at": _iso(row.last_synced_at),
        "last_error": row.last_error,
        "metadata": row.metadata_json or {},
        "capability": FeishuCapability.MANAGE_DOCUMENT_ACL.value,
    }


def _document_binding(db: Session, case_id: str) -> FeishuEvidenceDocumentBinding | None:
    return db.scalar(select(FeishuEvidenceDocumentBinding).where(
        FeishuEvidenceDocumentBinding.case_id == case_id,
    ).limit(1))


@router.get("/cases/{case_id}/feishu-document-acl")
def get_document_acl_status(
    case_id: str,
    db: Session = Depends(get_db),
    _identity: AuthIdentity = Depends(_admin),
):
    if db.get(Case, case_id) is None:
        raise HTTPException(404, "CASE_NOT_FOUND")
    document = _document_binding(db, case_id)
    document_id = document.document_id if document else None
    row = None
    if document_id:
        row = db.scalar(select(FeishuDocumentAclBinding).where(
            FeishuDocumentAclBinding.case_id == case_id,
            FeishuDocumentAclBinding.document_id == document_id,
        ).limit(1))
    return _out(row, case_id=case_id, document_id=document_id)


@router.post("/cases/{case_id}/feishu-document-acl/sync")
def request_document_acl_sync(
    case_id: str,
    req: ManualAclSyncRequest,
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    identity: AuthIdentity = Depends(_admin),
):
    if db.get(Case, case_id) is None:
        raise HTTPException(404, "CASE_NOT_FOUND")
    document = _document_binding(db, case_id)
    if document is None or not document.document_id:
        raise HTTPException(409, "FEISHU_DOCUMENT_NOT_BOUND")
    document_id = str(document.document_id)
    handle = begin_idempotent(
        db,
        scope=f"POST:/api/v1/cases/{case_id}/feishu-document-acl/sync",
        key=idempotency_key,
        payload={"case_id": case_id, "document_id": document_id, **req.model_dump(mode="json")},
    )
    if handle.replay is not None:
        return handle.replay

    row = db.scalar(select(FeishuDocumentAclBinding).where(
        FeishuDocumentAclBinding.case_id == case_id,
        FeishuDocumentAclBinding.document_id == document_id,
    ).limit(1))
    if row is not None and req.force_revision:
        row.desired_revision += 1
        row.status = "PENDING"
        row.last_error = None
        db.flush()

    async_result = sync_document_acl.delay(case_id, document_id)
    response = {
        "case_id": case_id,
        "document_id": document_id,
        "status": "QUEUED",
        "task_id": str(getattr(async_result, "id", "") or ""),
        "force_revision": req.force_revision,
        "desired_revision": row.desired_revision if row is not None else None,
        "capability": FeishuCapability.MANAGE_DOCUMENT_ACL.value,
    }
    audit(
        db,
        case_id=case_id,
        actor=identity.actor_id,
        event_type="FEISHU_DOCUMENT_ACL_SYNC_REQUESTED",
        target_type="feishu_document_acl",
        target_id=row.id if row is not None else document_id,
        detail={
            "schema_version": "feishu-document-acl-management-v1",
            "document_id": document_id,
            "force_revision": req.force_revision,
            "capability": FeishuCapability.MANAGE_DOCUMENT_ACL.value,
            "task_id": response["task_id"],
        },
    )
    complete_idempotent(
        db,
        handle,
        response=response,
        status_code=200,
        resource_type="feishu_document_acl",
        resource_id=row.id if row is not None else document_id,
    )
    db.commit()
    return response
