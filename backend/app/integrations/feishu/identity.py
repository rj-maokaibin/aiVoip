from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.contracts.enums import UserRole
from app.db.feishu_governance_models import FeishuUserIdentity


ACTIVE = "ACTIVE"
DISABLED = "DISABLED"
PENDING_MAPPING = "PENDING_MAPPING"


@dataclass(frozen=True)
class FeishuIdentityContext:
    tenant_key: str
    open_id: str
    actor_id: str | None
    role: UserRole | None
    status: str
    identity_id: str | None
    resolution_source: str

    @property
    def active(self) -> bool:
        return self.status == ACTIVE and self.actor_id is not None and self.role is not None


def _role(value: str | None) -> UserRole | None:
    try:
        return UserRole(str(value or ""))
    except ValueError:
        return None


def resolve_feishu_identity(
    db: Session, *, tenant_key: str | None, open_id: str | None,
    discover_unmapped: bool = True,
) -> FeishuIdentityContext:
    """Resolve Feishu sender identity without granting implicit permissions.

    Unknown senders may be materialized as PENDING_MAPPING for Admin discovery, but
    this never grants a role/capability. Tenant and open_id are both required;
    cross-tenant open_id reuse is intentionally isolated by the unique key.
    """
    tenant = str(tenant_key or "")
    oid = str(open_id or "")
    if not tenant or not oid:
        return FeishuIdentityContext(
            tenant_key=tenant, open_id=oid, actor_id=None, role=None,
            status=PENDING_MAPPING, identity_id=None,
            resolution_source="MISSING_TENANT_OR_OPEN_ID",
        )

    row = db.scalar(select(FeishuUserIdentity).where(
        FeishuUserIdentity.tenant_key == tenant,
        FeishuUserIdentity.open_id == oid,
    ).limit(1))
    now = datetime.now(timezone.utc)
    if row is None and discover_unmapped:
        candidate = FeishuUserIdentity(
            tenant_key=tenant,
            open_id=oid,
            internal_actor_id=f"feishu:{oid}",
            role=UserRole.VIEWER.value,
            status=PENDING_MAPPING,
            metadata_json={"discovered_by": "feishu-event"},
            last_seen_at=now,
        )
        try:
            with db.begin_nested():
                db.add(candidate)
                db.flush()
            row = candidate
        except IntegrityError:
            row = db.scalar(select(FeishuUserIdentity).where(
                FeishuUserIdentity.tenant_key == tenant,
                FeishuUserIdentity.open_id == oid,
            ).limit(1))

    if row is None:
        return FeishuIdentityContext(
            tenant_key=tenant, open_id=oid, actor_id=None, role=None,
            status=PENDING_MAPPING, identity_id=None,
            resolution_source="UNMAPPED",
        )

    row.last_seen_at = now
    parsed_role = _role(row.role)
    status = str(row.status or PENDING_MAPPING).upper()
    if parsed_role is None and status == ACTIVE:
        status = PENDING_MAPPING
    db.flush()
    return FeishuIdentityContext(
        tenant_key=tenant,
        open_id=oid,
        actor_id=row.internal_actor_id if status == ACTIVE else None,
        role=parsed_role if status == ACTIVE else None,
        status=status,
        identity_id=row.id,
        resolution_source="PERSISTED_MAPPING" if status == ACTIVE else status,
    )
