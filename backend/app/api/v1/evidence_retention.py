from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.api.evidence_permissions import EvidencePermission, require_evidence_permission
from app.db.evidence_retention_models import EvidenceRetentionState
from app.db.models import Evidence
from app.services.evidence_retention import (
    ensure_retention_state, expire_due_evidence, lock_evidence, retention_status, unlock_evidence,
)

router = APIRouter(tags=["evidence-retention"])


class EvidenceLockRequest(BaseModel):
    reason: str = Field(min_length=2, max_length=1000)


class EvidenceUnlockRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=1000)


@router.get("/evidences/{evidence_id}/retention")
def get_retention(
    evidence_id: str,
    db: Session = Depends(get_db),
    _identity=Depends(require_evidence_permission(EvidencePermission.VIEW_REPORT)),
):
    try:
        result = retention_status(db, evidence_id)
        db.commit()
        return result
    except ValueError as exc:
        db.rollback()
        raise HTTPException(404, str(exc))


@router.get("/cases/{case_id}/evidence-retention")
def list_case_retention(
    case_id: str,
    db: Session = Depends(get_db),
    _identity=Depends(require_evidence_permission(EvidencePermission.VIEW_REPORT)),
):
    evidences = list(db.scalars(select(Evidence).where(Evidence.case_id == case_id).order_by(Evidence.created_at.asc())))
    result = []
    for evidence in evidences:
        row = ensure_retention_state(db, evidence)
        result.append({
            "evidence_id": evidence.id,
            "filename": evidence.filename,
            "kind": evidence.kind,
            "policy": row.policy,
            "status": row.status,
            "retain_until": row.retain_until.isoformat() if row.retain_until else None,
            "golden_exempt": bool(row.golden_exempt),
            "locked_by": row.locked_by,
            "expired_at": row.expired_at.isoformat() if row.expired_at else None,
            "payload_available": row.status != "EXPIRED",
        })
    db.commit()
    return result


@router.post("/evidences/{evidence_id}/retention/lock")
def lock_retention(
    evidence_id: str,
    req: EvidenceLockRequest,
    db: Session = Depends(get_db),
    identity=Depends(require_evidence_permission(EvidencePermission.MANAGE_RETENTION)),
):
    try:
        row = lock_evidence(db, evidence_id=evidence_id, actor=identity.actor_id, reason=req.reason)
        db.commit()
        return retention_status(db, row.evidence_id)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(409 if "EXPIRED" in str(exc) else 404, str(exc))


@router.post("/evidences/{evidence_id}/retention/unlock")
def unlock_retention(
    evidence_id: str,
    req: EvidenceUnlockRequest,
    db: Session = Depends(get_db),
    identity=Depends(require_evidence_permission(EvidencePermission.MANAGE_RETENTION)),
):
    try:
        row = unlock_evidence(db, evidence_id=evidence_id, actor=identity.actor_id, reason=req.reason)
        db.commit()
        return retention_status(db, row.evidence_id)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(409 if "GOLDEN" in str(exc) or "EXPIRED" in str(exc) else 404, str(exc))


@router.post("/system/evidence-retention/run")
def run_retention(
    db: Session = Depends(get_db),
    identity=Depends(require_evidence_permission(EvidencePermission.MANAGE_RETENTION)),
):
    result = expire_due_evidence(db, actor=identity.actor_id)
    db.commit()
    return {"status": "SUCCESS", **result}
