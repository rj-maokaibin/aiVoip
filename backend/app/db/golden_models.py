from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.ids import new_id
from app.db.base import Base


def utcnow():
    return datetime.now(timezone.utc)


class GoldenCandidateAssessment(Base):
    """Latest deterministic Golden eligibility assessment for one Case.

    This is deliberately separate from AIProposal/Knowledge.  It is a materialized
    quality state derived from Case evidence, deterministic diagnosis and audit
    history.  One row per Case is updated in-place; transitions are also written to
    AuditLog, so the current state is queryable while the full history remains
    immutable/auditable.
    """

    __tablename__ = "golden_candidate_assessments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str] = mapped_column(
        ForeignKey("cases.id", ondelete="CASCADE"), unique=True, index=True
    )
    status: Mapped[str] = mapped_column(String(32), index=True, default="NOT_ELIGIBLE")
    verification_tier: Mapped[str | None] = mapped_column(String(8), nullable=True, index=True)
    assessment_version: Mapped[str] = mapped_column(String(64), default="golden-candidate-v1")
    score: Mapped[int] = mapped_column(Integer, default=0)

    root_cause_confirmed: Mapped[int] = mapped_column(Integer, default=0)
    fix_verified: Mapped[int] = mapped_column(Integer, default=0)
    direct_l1_support: Mapped[int] = mapped_column(Integer, default=0)
    deterministic_baseline_ready: Mapped[int] = mapped_column(Integer, default=0)
    snapshot_ready: Mapped[int] = mapped_column(Integer, default=0)
    audit_coverage_complete: Mapped[int] = mapped_column(Integer, default=0)
    answer_leakage_risk: Mapped[int] = mapped_column(Integer, default=0)

    evidence_count: Mapped[int] = mapped_column(Integer, default=0)
    complete_evidence_count: Mapped[int] = mapped_column(Integer, default=0)
    l1_evidence_count: Mapped[int] = mapped_column(Integer, default=0)
    successful_analyzer_count: Mapped[int] = mapped_column(Integer, default=0)
    confirmed_hypothesis_count: Mapped[int] = mapped_column(Integer, default=0)

    blocker_codes: Mapped[list] = mapped_column(JSON, default=list)
    gap_codes: Mapped[list] = mapped_column(JSON, default=list)
    next_steps: Mapped[list] = mapped_column(JSON, default=list)
    leakage_findings: Mapped[list] = mapped_column(JSON, default=list)
    details_json: Mapped[dict] = mapped_column(JSON, default=dict)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    assessed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
