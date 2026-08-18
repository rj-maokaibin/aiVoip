from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.feishu_permissions import FeishuCapability, authorize_capability
from app.contracts.enums import UserRole
from app.db.base import Base
from app.db.feishu_governance_models import FeishuUserIdentity
from app.db.models import Case
from app.integrations.feishu.identity import PENDING_MAPPING, resolve_feishu_identity


def _engine():
    # Importing the governance model above registers its tables in Base.metadata.
    return create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )


def _db():
    engine = _engine()
    Base.metadata.create_all(engine)
    return Session(engine)


def _identity(db: Session, *, tenant: str, open_id: str, role: UserRole, status: str = "ACTIVE"):
    row = FeishuUserIdentity(
        tenant_key=tenant,
        open_id=open_id,
        internal_actor_id=f"actor:{tenant}:{open_id}",
        role=role.value,
        status=status,
    )
    db.add(row)
    db.flush()
    return row


def test_unknown_identity_is_discovered_but_never_implicitly_authorized():
    with _db() as db:
        context = resolve_feishu_identity(
            db, tenant_key="tenant-a", open_id="ou-new", discover_unmapped=True,
        )
        assert context.active is False
        assert context.status == PENDING_MAPPING
        stored = db.scalar(select(FeishuUserIdentity).where(
            FeishuUserIdentity.tenant_key == "tenant-a",
            FeishuUserIdentity.open_id == "ou-new",
        ))
        assert stored is not None
        assert stored.status == PENDING_MAPPING
        assert stored.role == UserRole.VIEWER.value


def test_same_open_id_isolated_by_tenant():
    with _db() as db:
        _identity(db, tenant="tenant-a", open_id="ou-1", role=UserRole.VIEWER)
        _identity(db, tenant="tenant-b", open_id="ou-1", role=UserRole.ENGINEER)
        a = resolve_feishu_identity(db, tenant_key="tenant-a", open_id="ou-1")
        b = resolve_feishu_identity(db, tenant_key="tenant-b", open_id="ou-1")
        assert a.role == UserRole.VIEWER
        assert b.role == UserRole.ENGINEER
        assert a.actor_id != b.actor_id


def test_disabled_identity_is_fail_closed():
    with _db() as db:
        _identity(db, tenant="tenant-a", open_id="ou-disabled", role=UserRole.ADMIN, status="DISABLED")
        context = resolve_feishu_identity(db, tenant_key="tenant-a", open_id="ou-disabled")
        assert context.active is False
        assert context.actor_id is None
        assert context.role is None


def test_role_capability_matrix_keeps_control_away_from_viewer():
    with _db() as db:
        case = Case(case_no="CASE-RBAC", summary="DTMF", status="ANALYZING")
        db.add(case)
        db.flush()
        _identity(db, tenant="tenant-a", open_id="ou-viewer", role=UserRole.VIEWER)
        _identity(db, tenant="tenant-a", open_id="ou-engineer", role=UserRole.ENGINEER)
        viewer = resolve_feishu_identity(db, tenant_key="tenant-a", open_id="ou-viewer")
        engineer = resolve_feishu_identity(db, tenant_key="tenant-a", open_id="ou-engineer")

        assert authorize_capability(
            db, identity=viewer, capability=FeishuCapability.VIEW_CASE, case_id=case.id,
        ).allowed is True
        assert authorize_capability(
            db, identity=viewer, capability=FeishuCapability.ADD_EVIDENCE, case_id=case.id,
        ).allowed is True
        denied = authorize_capability(
            db, identity=viewer, capability=FeishuCapability.CONTROL_REPRODUCTION, case_id=case.id,
        )
        assert denied.allowed is False
        assert denied.reason == "GLOBAL_ROLE_MISSING_CAPABILITY"

        assert authorize_capability(
            db, identity=engineer, capability=FeishuCapability.CONTROL_REPRODUCTION, case_id=case.id,
        ).allowed is True


def test_stale_or_forged_case_id_is_denied_without_invalid_audit_fk():
    with _db() as db:
        _identity(db, tenant="tenant-a", open_id="ou-engineer", role=UserRole.ENGINEER)
        engineer = resolve_feishu_identity(db, tenant_key="tenant-a", open_id="ou-engineer")
        decision = authorize_capability(
            db,
            identity=engineer,
            capability=FeishuCapability.CONTROL_REPRODUCTION,
            case_id="missing-case-id",
        )
        assert decision.allowed is False
        assert decision.reason == "CASE_NOT_FOUND"
        assert decision.case_id == "missing-case-id"
        # Audit uses case_id=None internally when the requested Case does not exist,
        # so flushing the decision must remain valid on FK-enforcing databases.
        db.flush()
