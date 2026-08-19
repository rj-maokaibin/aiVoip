from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.feishu_permissions import FeishuCapability, authorize_capability
from app.contracts.enums import UserRole
from app.db.base import Base
from app.db.feishu_governance_models import CaseAclEntry, FeishuUserIdentity
from app.db.models import Case
from app.integrations.feishu.identity import resolve_feishu_identity


def _db():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return Session(engine)


def _setup(db: Session, role: UserRole, *, created_by: str | None = None):
    actor = f"actor:{role.value.lower()}"
    case = Case(case_no=f"CASE-{role.value}", summary="单通", status="ANALYZING", created_by=created_by)
    identity = FeishuUserIdentity(
        tenant_key="tenant-a", open_id=f"ou-{role.value.lower()}",
        internal_actor_id=actor, role=role.value, status="ACTIVE",
    )
    db.add_all([case, identity])
    db.flush()
    context = resolve_feishu_identity(db, tenant_key="tenant-a", open_id=identity.open_id)
    return case, context


def test_case_acl_deny_overrides_engineer_global_role():
    with _db() as db:
        case, identity = _setup(db, UserRole.ENGINEER)
        db.add(CaseAclEntry(
            case_id=case.id, actor_id=identity.actor_id,
            capability=FeishuCapability.CONTROL_REPRODUCTION.value,
            effect="DENY", created_by="admin",
        ))
        db.flush()
        decision = authorize_capability(
            db, identity=identity,
            capability=FeishuCapability.CONTROL_REPRODUCTION,
            case_id=case.id,
        )
        assert decision.allowed is False
        assert decision.acl_effect == "DENY"
        assert decision.reason == "CASE_ACL_DENY"


def test_case_acl_allow_does_not_elevate_viewer_beyond_global_role():
    with _db() as db:
        case, identity = _setup(db, UserRole.VIEWER)
        db.add(CaseAclEntry(
            case_id=case.id, actor_id=identity.actor_id,
            capability=FeishuCapability.CONTROL_REPRODUCTION.value,
            effect="ALLOW", created_by="admin",
        ))
        db.flush()
        decision = authorize_capability(
            db, identity=identity,
            capability=FeishuCapability.CONTROL_REPRODUCTION,
            case_id=case.id,
        )
        assert decision.allowed is False
        assert decision.reason == "GLOBAL_ROLE_MISSING_CAPABILITY"


def test_expired_case_acl_deny_no_longer_blocks_global_permission():
    with _db() as db:
        case, identity = _setup(db, UserRole.ENGINEER)
        db.add(CaseAclEntry(
            case_id=case.id, actor_id=identity.actor_id,
            capability=FeishuCapability.CONTROL_REPRODUCTION.value,
            effect="DENY", created_by="admin",
            expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        ))
        db.flush()
        decision = authorize_capability(
            db, identity=identity,
            capability=FeishuCapability.CONTROL_REPRODUCTION,
            case_id=case.id,
        )
        assert decision.allowed is True
        assert decision.acl_effect is None


def test_case_owner_overlay_never_upgrades_viewer_authority():
    with _db() as db:
        actor = "actor:viewer"
        case, identity = _setup(db, UserRole.VIEWER, created_by=actor)
        assert identity.actor_id == actor
        decision = authorize_capability(
            db, identity=identity,
            capability=FeishuCapability.REVIEW_ROOT_CAUSE,
            case_id=case.id,
        )
        assert decision.case_owner is True
        assert decision.allowed is False
