from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.ids import new_id
from app.db.base import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class EvidenceRetentionState(Base):
    """Materialized retention state for raw Evidence.

    The Evidence row remains as immutable metadata/provenance after payload expiry.
    Only the object payload is removed. Derived key artifacts/reports are governed
    separately and are intentionally long-lived for Preliminary Evidence Report V1.
    """

    __tablename__ = "evidence_retention_states"
    __table_args__ = (UniqueConstraint("evidence_id", name="uq_evidence_retention_evidence"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    evidence_id: Mapped[str] = mapped_column(ForeignKey("evidences.id", ondelete="CASCADE"), index=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"), index=True)
    policy: Mapped[str] = mapped_column(String(32), default="STANDARD_90D", index=True)
    retain_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    locked_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lock_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    golden_exempt: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    expired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    object_deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="ACTIVE", index=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
