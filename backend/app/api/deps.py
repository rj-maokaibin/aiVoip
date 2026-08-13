from __future__ import annotations

from typing import Callable

from fastapi import Depends, Header

from app.contracts.enums import PermissionName, UserRole
from app.core.config import settings
from app.auth.providers import AuthIdentity, AuthRequest, get_auth_provider
from app.core.errors import AppError


def get_db():
    # Lazy import keeps contract/auth helpers usable in offline unit tests and CLI
    # tools that do not have the PostgreSQL driver/runtime installed.
    from app.db.session import SessionLocal
    db=SessionLocal()
    try: yield db
    finally: db.close()


ROLE_PERMISSIONS: dict[UserRole, frozenset[PermissionName]] = {
    UserRole.VIEWER: frozenset({
        PermissionName.CASE_READ, PermissionName.EVIDENCE_READ, PermissionName.JOB_READ,
        PermissionName.DIAGNOSIS_READ, PermissionName.RULE_READ, PermissionName.KNOWLEDGE_READ,
        PermissionName.REPORT_READ, PermissionName.AUDIT_READ, PermissionName.REPRODUCTION_READ,
        PermissionName.EXPERIMENT_READ, PermissionName.FIX_READ,
    }),
    UserRole.ENGINEER: frozenset({
        PermissionName.CASE_READ, PermissionName.CASE_WRITE, PermissionName.EVIDENCE_READ,
        PermissionName.EVIDENCE_UPLOAD, PermissionName.JOB_READ, PermissionName.JOB_CONTROL,
        PermissionName.DIAGNOSIS_READ, PermissionName.DIAGNOSIS_RUN, PermissionName.RULE_READ,
        PermissionName.RULE_EDIT, PermissionName.KNOWLEDGE_READ, PermissionName.KNOWLEDGE_EDIT,
        PermissionName.REPORT_READ, PermissionName.REPORT_GENERATE, PermissionName.AUDIT_READ,
        PermissionName.REPRODUCTION_READ, PermissionName.REPRODUCTION_CONTROL,
        PermissionName.EXPERIMENT_READ, PermissionName.EXPERIMENT_CONTROL, PermissionName.FIX_READ, PermissionName.FIX_CONTROL,
    }),
    UserRole.EXPERT_REVIEWER: frozenset({
        PermissionName.CASE_READ, PermissionName.CASE_WRITE, PermissionName.EVIDENCE_READ,
        PermissionName.EVIDENCE_UPLOAD, PermissionName.JOB_READ, PermissionName.JOB_CONTROL,
        PermissionName.DIAGNOSIS_READ, PermissionName.DIAGNOSIS_RUN, PermissionName.RULE_READ,
        PermissionName.RULE_EDIT, PermissionName.RULE_APPROVE, PermissionName.KNOWLEDGE_READ,
        PermissionName.KNOWLEDGE_EDIT, PermissionName.KNOWLEDGE_VERIFY, PermissionName.REPORT_READ,
        PermissionName.REPORT_GENERATE, PermissionName.AUDIT_READ,
        PermissionName.REPRODUCTION_READ, PermissionName.REPRODUCTION_CONTROL,
        PermissionName.EXPERIMENT_READ, PermissionName.EXPERIMENT_CONTROL, PermissionName.FIX_READ, PermissionName.FIX_CONTROL,
    }),
    UserRole.ADMIN: frozenset(PermissionName),
    UserRole.SERVICE: frozenset({
        PermissionName.CASE_READ, PermissionName.CASE_WRITE, PermissionName.EVIDENCE_READ,
        PermissionName.EVIDENCE_UPLOAD, PermissionName.JOB_READ, PermissionName.JOB_CONTROL,
        PermissionName.DIAGNOSIS_READ, PermissionName.DIAGNOSIS_RUN, PermissionName.RULE_READ,
        PermissionName.KNOWLEDGE_READ, PermissionName.REPORT_READ, PermissionName.REPORT_GENERATE,
        PermissionName.AUDIT_READ, PermissionName.REPRODUCTION_READ, PermissionName.REPRODUCTION_CONTROL,
        PermissionName.EXPERIMENT_READ, PermissionName.EXPERIMENT_CONTROL, PermissionName.FIX_READ, PermissionName.FIX_CONTROL,
    }),
}


def get_identity(
    x_actor_id: str | None = Header(default=None, alias='X-Actor-Id'),
    x_actor_role: str | None = Header(default=None, alias='X-Actor-Role'),
    x_auth_timestamp: str | None = Header(default=None, alias='X-Auth-Timestamp'),
    x_auth_signature: str | None = Header(default=None, alias='X-Auth-Signature'),
) -> AuthIdentity:
    provider = get_auth_provider()
    return provider.authenticate(AuthRequest(
        actor_id=x_actor_id, actor_role=x_actor_role,
        timestamp=x_auth_timestamp, signature=x_auth_signature,
    ))


def require_roles(*allowed: UserRole) -> Callable:
    allowed_set = {UserRole(x) for x in allowed}
    def dependency(identity: AuthIdentity = Depends(get_identity)) -> AuthIdentity:
        if identity.role not in allowed_set:
            raise AppError('PERMISSION_DENIED', details={'required_roles':[x.value for x in sorted(allowed_set,key=lambda y:y.value)],'actual_role':identity.role.value})
        return identity
    return dependency


def require_permissions(*required: PermissionName) -> Callable:
    required_set = {PermissionName(x) for x in required}
    def dependency(identity: AuthIdentity = Depends(get_identity)) -> AuthIdentity:
        granted = ROLE_PERMISSIONS.get(identity.role, frozenset())
        missing = sorted(x.value for x in (required_set - granted))
        if missing:
            raise AppError(
                'PERMISSION_DENIED',
                details={
                    'required_permissions': sorted(x.value for x in required_set),
                    'missing_permissions': missing,
                    'actual_role': identity.role.value,
                },
            )
        return identity
    return dependency


READ_ROLES = (UserRole.VIEWER, UserRole.ENGINEER, UserRole.EXPERT_REVIEWER, UserRole.ADMIN, UserRole.SERVICE)
ENGINEER_ROLES = (UserRole.ENGINEER, UserRole.EXPERT_REVIEWER, UserRole.ADMIN, UserRole.SERVICE)
REVIEWER_ROLES = (UserRole.EXPERT_REVIEWER, UserRole.ADMIN)
ADMIN_ROLES = (UserRole.ADMIN,)
