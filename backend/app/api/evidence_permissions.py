from __future__ import annotations

from enum import StrEnum
from typing import Callable

from fastapi import Depends

from app.auth.providers import AuthIdentity
from app.contracts.enums import UserRole
from app.core.errors import AppError
from app.api.deps import get_identity


class EvidencePermission(StrEnum):
    VIEW_REPORT = "VIEW_REPORT"
    VIEW_RAW_EVIDENCE = "VIEW_RAW_EVIDENCE"
    DOWNLOAD_EVIDENCE_BUNDLE = "DOWNLOAD_EVIDENCE_BUNDLE"
    REBUILD_REPORT = "REBUILD_REPORT"
    MANAGE_RETENTION = "MANAGE_RETENTION"
    VIEW_PIPELINE_METRICS = "VIEW_PIPELINE_METRICS"


ROLE_EVIDENCE_PERMISSIONS: dict[UserRole, frozenset[EvidencePermission]] = {
    UserRole.VIEWER: frozenset({
        EvidencePermission.VIEW_REPORT,
    }),
    UserRole.ENGINEER: frozenset({
        EvidencePermission.VIEW_REPORT,
        EvidencePermission.VIEW_RAW_EVIDENCE,
        EvidencePermission.DOWNLOAD_EVIDENCE_BUNDLE,
        EvidencePermission.REBUILD_REPORT,
        EvidencePermission.VIEW_PIPELINE_METRICS,
    }),
    UserRole.EXPERT_REVIEWER: frozenset({
        EvidencePermission.VIEW_REPORT,
        EvidencePermission.VIEW_RAW_EVIDENCE,
        EvidencePermission.DOWNLOAD_EVIDENCE_BUNDLE,
        EvidencePermission.REBUILD_REPORT,
        EvidencePermission.MANAGE_RETENTION,
        EvidencePermission.VIEW_PIPELINE_METRICS,
    }),
    UserRole.ADMIN: frozenset(EvidencePermission),
    UserRole.SERVICE: frozenset({
        EvidencePermission.VIEW_REPORT,
        EvidencePermission.VIEW_RAW_EVIDENCE,
        EvidencePermission.DOWNLOAD_EVIDENCE_BUNDLE,
        EvidencePermission.REBUILD_REPORT,
        EvidencePermission.MANAGE_RETENTION,
        EvidencePermission.VIEW_PIPELINE_METRICS,
    }),
}


def has_evidence_permission(identity: AuthIdentity, permission: EvidencePermission) -> bool:
    return permission in ROLE_EVIDENCE_PERMISSIONS.get(identity.role, frozenset())


def require_evidence_permission(permission: EvidencePermission) -> Callable:
    def dependency(identity: AuthIdentity = Depends(get_identity)) -> AuthIdentity:
        if not has_evidence_permission(identity, permission):
            raise AppError(
                "PERMISSION_DENIED",
                details={
                    "required_evidence_permission": permission.value,
                    "actual_role": identity.role.value,
                },
            )
        return identity
    return dependency
