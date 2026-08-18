from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.ids import new_id
from app.db.base import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class FeishuUserIdentity(Base):
    __tablename__ = "feishu_user_identities"
    __table_args__ = (
        UniqueConstraint("tenant_key", "open_id", name="uq_feishu_identity_tenant_open_id"),
        Index("ix_feishu_identity_tenant", "tenant_key"),
        Index("ix_feishu_identity_open_id", "open_id"),
        Index("ix_feishu_identity_union_id", "union_id"),
        Index("ix_feishu_identity_user_id", "user_id"),
        Index("ix_feishu_identity_actor", "internal_actor_id"),
        Index("ix_feishu_identity_role", "role"),
        Index("ix_feishu_identity_status", "status"),
        Index("ix_feishu_identity_last_seen", "last_seen_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_key: Mapped[str] = mapped_column(String(128), nullable=False)
    open_id: Mapped[str] = mapped_column(String(128), nullable=False)
    union_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    internal_actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="ACTIVE")
    display_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class CaseAclEntry(Base):
    __tablename__ = "case_acl_entries"
    __table_args__ = (
        UniqueConstraint("case_id", "actor_id", "capability", name="uq_case_acl_actor_capability"),
        CheckConstraint("effect IN ('ALLOW','DENY')", name="ck_case_acl_effect"),
        Index("ix_case_acl_case", "case_id"),
        Index("ix_case_acl_actor", "actor_id"),
        Index("ix_case_acl_capability", "capability"),
        Index("ix_case_acl_effect", "effect"),
        Index("ix_case_acl_expires", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    capability: Mapped[str] = mapped_column(String(64), nullable=False)
    effect: Mapped[str] = mapped_column(String(8), nullable=False)  # ALLOW / DENY
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)