from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.contracts.enums import UserRole
from app.db.feishu_governance_models import CaseAclEntry
from app.db.models import Case
from app.integrations.feishu.identity import FeishuIdentityContext
from app.services.audit import audit


class FeishuCapability(StrEnum):
    VIEW_CASE = "VIEW_CASE"
    VIEW_REPORT = "VIEW_REPORT"
    VIEW_RAW_EVIDENCE = "VIEW_RAW_EVIDENCE"
    DOWNLOAD_EVIDENCE_BUNDLE = "DOWNLOAD_EVIDENCE_BUNDLE"
    ADD_EVIDENCE = "ADD_EVIDENCE"
    REBUILD_REPORT = "REBUILD_REPORT"
    CONTROL_REPRODUCTION = "CONTROL_REPRODUCTION"
    COMPLETE_EXTERNAL_ACTION = "COMPLETE_EXTERNAL_ACTION"
    MARK_FIX_APPLIED = "MARK_FIX_APPLIED"
    RUN_REGISTERED_EXPERIMENT = "RUN_REGISTERED_EXPERIMENT"
    RUN_AI_SUGGESTION = "RUN_AI_SUGGESTION"
    MANAGE_CASE_BINDING = "MANAGE_CASE_BINDING"
    MANAGE_FEISHU_IDENTITY = "MANAGE_FEISHU_IDENTITY"
    MANAGE_DOCUMENT_ACL = "MANAGE_DOCUMENT_ACL"
    MANAGE_RETENTION = "MANAGE_RETENTION"
    REVIEW_ROOT_CAUSE = "REVIEW_ROOT_CAUSE"


ROLE_CAPABILITIES: dict[UserRole, frozenset[FeishuCapability]] = {
    UserRole.VIEWER: frozenset({
        FeishuCapability.VIEW_CASE,
        FeishuCapability.VIEW_REPORT,
        FeishuCapability.ADD_EVIDENCE,
    }),
    UserRole.ENGINEER: frozenset({
        FeishuCapability.VIEW_CASE,
        FeishuCapability.VIEW_REPORT,
        FeishuCapability.VIEW_RAW_EVIDENCE,
        FeishuCapability.DOWNLOAD_EVIDENCE_BUNDLE,
        FeishuCapability.ADD_EVIDENCE,
        FeishuCapability.REBUILD_REPORT,
        FeishuCapability.CONTROL_REPRODUCTION,
        FeishuCapability.COMPLETE_EXTERNAL_ACTION,
        FeishuCapability.MARK_FIX_APPLIED,
        FeishuCapability.RUN_REGISTERED_EXPERIMENT,
        FeishuCapability.RUN_AI_SUGGESTION,
    }),
    UserRole.EXPERT_REVIEWER: frozenset({
        FeishuCapability.VIEW_CASE,
        FeishuCapability.VIEW_REPORT,
        FeishuCapability.VIEW_RAW_EVIDENCE,
        FeishuCapability.DOWNLOAD_EVIDENCE_BUNDLE,
        FeishuCapability.ADD_EVIDENCE,
        FeishuCapability.REBUILD_REPORT,
        FeishuCapability.CONTROL_REPRODUCTION,
        FeishuCapability.COMPLETE_EXTERNAL_ACTION,
        FeishuCapability.MARK_FIX_APPLIED,
        FeishuCapability.RUN_REGISTERED_EXPERIMENT,
        FeishuCapability.RUN_AI_SUGGESTION,
        FeishuCapability.MANAGE_RETENTION,
        FeishuCapability.REVIEW_ROOT_CAUSE,
    }),
    UserRole.ADMIN: frozenset(FeishuCapability),
    UserRole.SERVICE: frozenset(FeishuCapability),
}


INTENT_CAPABILITY: dict[str, FeishuCapability] = {
    "NEW_DIAGNOSIS": FeishuCapability.ADD_EVIDENCE,
    "CASE_FOLLOW_UP": FeishuCapability.ADD_EVIDENCE,
    "STATUS_QUERY": FeishuCapability.VIEW_CASE,
    "GENERAL_QUESTION": FeishuCapability.VIEW_CASE,
    "STOP_REPRODUCTION": FeishuCapability.CONTROL_REPRODUCTION,
    "EXTERNAL_ACTION_COMPLETED": FeishuCapability.COMPLETE_EXTERNAL_ACTION,
    "FIX_APPLIED": FeishuCapability.MARK_FIX_APPLIED,
    "START_REGISTERED_EXPERIMENT": FeishuCapability.RUN_REGISTERED_EXPERIMENT,
    "DOWNLOAD_BUNDLE": FeishuCapability.DOWNLOAD_EVIDENCE_BUNDLE,
    "REBUILD_REPORT": FeishuCapability.REBUILD_REPORT,
    "RETENTION_LOCK": FeishuCapability.MANAGE_RETENTION,
    "ROOT_CAUSE_CONFIRM": FeishuCapability.REVIEW_ROOT_CAUSE,
}


@dataclass(frozen=True)
class FeishuAuthorizationDecision:
    allowed: bool
    capability: FeishuCapability | None
    actor_id: str | None
    role: str | None
    identity_status: str
    case_id: str | None
    acl_effect: str | None
    case_owner: bool
    reason: str

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "capability": self.capability.value if self.capability else None,
            "actor_id": self.actor_id,
            "role": self.role,
            "identity_status": self.identity_status,
            "case_id": self.case_id,
            "acl_effect": self.acl_effect,
            "case_owner": self.case_owner,
            "reason": self.reason,
        }


def capability_for_intent(intent: str | None) -> FeishuCapability | None:
    return INTENT_CAPABILITY.get(str(intent or "").upper())


def _not_expired(value) -> bool:
    if value is None:
        return True
    now = datetime.now(timezone.utc)
    if value.tzinfo is None:
        now = now.replace(tzinfo=None)
    return value > now


def authorize_capability(
    db: Session, *, identity: FeishuIdentityContext,
    capability: FeishuCapability, case_id: str | None,
    audit_actor: str = "feishu-authorization",
) -> FeishuAuthorizationDecision:
    role_value = identity.role.value if identity.role else None
    case = db.get(Case, case_id) if case_id else None
    is_owner = bool(case and identity.actor_id and case.created_by == identity.actor_id)

    if not identity.active:
        decision = FeishuAuthorizationDecision(
            False, capability, None, role_value, identity.status, case_id,
            None, is_owner, "IDENTITY_NOT_ACTIVE",
        )
    elif case_id and case is None:
        decision = FeishuAuthorizationDecision(
            False, capability, identity.actor_id, role_value, identity.status,
            case_id, None, False, "CASE_NOT_FOUND",
        )
    elif capability not in ROLE_CAPABILITIES.get(identity.role, frozenset()):
        decision = FeishuAuthorizationDecision(
            False, capability, identity.actor_id, role_value, identity.status,
            case_id, None, is_owner, "GLOBAL_ROLE_MISSING_CAPABILITY",
        )
    else:
        acl_effect = None
        if case_id and identity.actor_id:
            row = db.scalar(select(CaseAclEntry).where(
                CaseAclEntry.case_id == case_id,
                CaseAclEntry.actor_id == identity.actor_id,
                CaseAclEntry.capability == capability.value,
            ).limit(1))
            if row and _not_expired(row.expires_at):
                acl_effect = str(row.effect or "").upper()
        if acl_effect == "DENY":
            decision = FeishuAuthorizationDecision(
                False, capability, identity.actor_id, role_value, identity.status,
                case_id, acl_effect, is_owner, "CASE_ACL_DENY",
            )
        else:
            decision = FeishuAuthorizationDecision(
                True, capability, identity.actor_id, role_value, identity.status,
                case_id, acl_effect, is_owner,
                "CASE_ACL_ALLOW" if acl_effect == "ALLOW" else ("CASE_OWNER_GLOBAL_ALLOWED" if is_owner else "GLOBAL_ROLE_ALLOWED"),
            )

    audit(
        db,
        case_id=case.id if case is not None else None,
        actor=identity.actor_id or audit_actor,
        event_type="AUTHORIZATION_DECIDED",
        target_type="feishu_authorization",
        target_id=identity.identity_id,
        detail={
            "schema_version": "feishu-authorization-v1",
            **decision.to_dict(),
        },
    )
    return decision


def authorize_intent(
    db: Session, *, identity: FeishuIdentityContext,
    intent: str, case_id: str | None,
) -> FeishuAuthorizationDecision:
    capability = capability_for_intent(intent)
    if capability is None:
        return FeishuAuthorizationDecision(
            True, None, identity.actor_id, identity.role.value if identity.role else None,
            identity.status, case_id, None, False, "NO_WORKFLOW_CAPABILITY_REQUIRED",
        )
    return authorize_capability(
        db, identity=identity, capability=capability, case_id=case_id,
    )