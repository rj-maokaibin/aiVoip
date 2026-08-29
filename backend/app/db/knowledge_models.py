from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.ids import new_id
from app.db.base import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ProductFact(Base):
    """Version/scope-aware strict product fact.

    This table is the preferred authority for statements such as support flags,
    maximum/minimum counts, protocol versions, defaults and numeric ranges.  It is
    intentionally separate from free-form KnowledgeItem/RAG documents so an LLM
    cannot silently turn an approximate document match into a strict product fact.
    """

    __tablename__ = "product_facts"
    __table_args__ = (
        UniqueConstraint(
            "product_model", "feature_key", "hw_scope", "sw_version_scope", "region_scope",
            "effective_from", name="uq_product_fact_scope_version"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    product_model: Mapped[str] = mapped_column(String(128), index=True)
    feature_key: Mapped[str] = mapped_column(String(256), index=True)
    value_json: Mapped[dict] = mapped_column(JSON)
    value_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    unit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    hw_scope: Mapped[str] = mapped_column(String(128), default="*", index=True)
    sw_version_scope: Mapped[str] = mapped_column(String(128), default="*", index=True)
    region_scope: Mapped[str] = mapped_column(String(128), default="*", index=True)
    source_document: Mapped[str] = mapped_column(String(512))
    source_section: Mapped[str | None] = mapped_column(String(512), nullable=True)
    source_ref: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    authority_level: Mapped[int] = mapped_column(Integer, default=2, index=True)
    approval_status: Mapped[str] = mapped_column(String(32), default="DRAFT", index=True)
    supersedes_fact_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    effective_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    approved_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
