from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.ids import new_id
from app.db.base import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class FeishuUserIdentity(Base):
    __tablename__ = "feishu_user_identities"
    __table_args__ = (
        UniqueConstraint("tenant_key", "open_id", name="uq_feishu_identity_tenant_open_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    open_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    union_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    user_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    internal_actor_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="ACTIVE", index=True)
    display_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class CaseAclEntry(Base):
    __tablename__ = "case_acl_entries"
    __table_args__ = (
        UniqueConstraint("case_id", "actor_id", "capability", name="uq_case_acl_actor_capability"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"), nullable=False, index=True)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    capability: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    effect: Mapped[str] = mapped_column(String(8), nullable=False, index=True)  # ALLOW / DENY
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
