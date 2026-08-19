from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.v1.feishu_governance import (
    CaseAclItem,
    IdentityPatchRequest,
    IdentityUpsertRequest,
    ReplaceCaseAclRequest,
    patch_identity,
    replace_case_acl,
    upsert_identity,
)
from app.auth.providers import AuthIdentity
from app.contracts.enums import UserRole
from app.db.base import Base
from app.db.feishu_governance_models import CaseAclEntry, FeishuUserIdentity
from app.db.models import Case, IdempotencyRecord


def _db():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return Session(engine)


def _admin():
    return AuthIdentity("admin-1", UserRole.ADMIN, True, "test")


def test_identity_upsert_and_patch_are_idempotent_and_persist_mapping():
    with _db() as db:
        request = IdentityUpsertRequest(
            tenant_key="tenant-a",
            open_id="ou-1",
            internal_actor_id="engineer-1",
            role=UserRole.ENGINEER,
            display_name="Engineer One",
        )
        first = upsert_identity(request, db=db, idempotency_key="idem-identity-1", identity=_admin())
        second = upsert_identity(request, db=db, idempotency_key="idem-identity-1", identity=_admin())
        assert second == first
        row = db.scalar(select(FeishuUserIdentity).where(
            FeishuUserIdentity.tenant_key == "tenant-a",
            FeishuUserIdentity.open_id == "ou-1",
        ))
        assert row is not None
        assert row.role == "ENGINEER"
        assert db.scalar(select(IdempotencyRecord).where(
            IdempotencyRecord.scope == "POST:/api/v1/feishu/identities"
        )).status == "COMPLETED"

        patched = patch_identity(
            row.id,
            IdentityPatchRequest(role=UserRole.EXPERT_REVIEWER),
            db=db,
            idempotency_key="idem-identity-patch-1",
            identity=_admin(),
        )
        assert patched["role"] == "EXPERT_REVIEWER"


def test_replace_case_acl_is_desired_state_and_idempotent():
    with _db() as db:
        case = Case(case_no="CASE-ACL-API", summary="电流音", status="ANALYZING")
        db.add(case)
        db.commit()
        request = ReplaceCaseAclRequest(entries=[
            CaseAclItem(
                actor_id="engineer-1",
                capability="CONTROL_REPRODUCTION",
                effect="DENY",
            )
        ])
        first = replace_case_acl(
            case.id, request, db=db,
            idempotency_key="idem-acl-1", identity=_admin(),
        )
        second = replace_case_acl(
            case.id, request, db=db,
            idempotency_key="idem-acl-1", identity=_admin(),
        )
        assert second == first
        rows = list(db.scalars(select(CaseAclEntry).where(CaseAclEntry.case_id == case.id)))
        assert len(rows) == 1
        assert rows[0].effect == "DENY"
        assert rows[0].capability == "CONTROL_REPRODUCTION"
