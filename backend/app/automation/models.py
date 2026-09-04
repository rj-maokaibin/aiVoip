from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.ids import new_id
from app.db.base import Base
from app.infrastructure.action_route import RunIntent


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AutomationTestRun(Base):
    __tablename__ = "automation_test_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str] = mapped_column(
        ForeignKey("cases.id", ondelete="CASCADE"), index=True
    )
    suite_id: Mapped[str] = mapped_column(String(128), index=True)
    case_key: Mapped[str] = mapped_column(String(128), index=True)
    case_version: Mapped[int] = mapped_column(Integer)
    case_checksum: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    intent: Mapped[str] = mapped_column(String(16), default=RunIntent.VERIFY.value, index=True)
    status: Mapped[str] = mapped_column(String(32), index=True, default="CREATED")
    environment_profile: Mapped[str] = mapped_column(String(128), index=True)
    effective_plan_json: Mapped[dict] = mapped_column(JSON)
    worker_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    authority_ref: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    verdict: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    terminal_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class AutomationTestStepRun(Base):
    __tablename__ = "automation_test_step_runs"
    __table_args__ = (
        UniqueConstraint("test_run_id", "step_no", name="uq_automation_test_step_no"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    test_run_id: Mapped[str] = mapped_column(
        ForeignKey("automation_test_runs.id", ondelete="CASCADE"), index=True
    )
    step_no: Mapped[int] = mapped_column(Integer)
    action_id: Mapped[str] = mapped_column(String(160), index=True)
    route_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    purpose: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    action_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("action_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    output_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AssertionResult(Base):
    __tablename__ = "assertion_results"
    __table_args__ = (
        UniqueConstraint("test_run_id", "assertion_no", name="uq_automation_assertion_no"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    test_run_id: Mapped[str] = mapped_column(
        ForeignKey("automation_test_runs.id", ondelete="CASCADE"), index=True
    )
    assertion_no: Mapped[int] = mapped_column(Integer)
    assertion_id: Mapped[str] = mapped_column(String(128), index=True)
    source: Mapped[str] = mapped_column(String(64), index=True)
    path: Mapped[str] = mapped_column(String(512))
    operator: Mapped[str] = mapped_column(String(32))
    expected_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    actual_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    verdict: Mapped[str] = mapped_column(String(32), index=True)
    evidence_refs_json: Mapped[list] = mapped_column(JSON)
    route_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    source_timestamp: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
