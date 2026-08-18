from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, CheckConstraint, DateTime, Float, ForeignKey, Index, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.ids import new_id
from app.db.base import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AISemanticIntentRecord(Base):
    __tablename__ = "ai_semantic_intent_records"
    __table_args__ = (
        UniqueConstraint("message_id", name="uq_ai_semantic_intent_message_id"),
        CheckConstraint(
            "status IN ('SHADOW_VALID','REJECTED','BYPASSED','GATEWAY_FAILED')",
            name="ck_ai_semantic_intent_status",
        ),
        Index("ix_ai_semantic_intent_case", "case_id"),
        Index("ix_ai_semantic_intent_chat", "tenant_key", "chat_id"),
        Index("ix_ai_semantic_intent_status", "status"),
        Index("ix_ai_semantic_intent_created", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str | None] = mapped_column(ForeignKey("cases.id", ondelete="SET NULL"), nullable=True)
    tenant_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    chat_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    message_id: Mapped[str] = mapped_column(String(256), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    deterministic_intent: Mapped[str] = mapped_column(String(48), nullable=False)
    deterministic_confidence: Mapped[float] = mapped_column(Float, nullable=False)
    proposal_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    validated_intent: Mapped[str | None] = mapped_column(String(48), nullable=True)
    ai_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False, default="feishu-semantic-router-v1")
    model_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AICaseCopilotRecord(Base):
    __tablename__ = "ai_case_copilot_records"
    __table_args__ = (
        UniqueConstraint("request_key", name="uq_ai_case_copilot_request_key"),
        CheckConstraint(
            "status IN ('ANSWERED','CONTROL_INTENT_REQUIRED','REJECTED','GATEWAY_FAILED')",
            name="ck_ai_case_copilot_status",
        ),
        Index("ix_ai_case_copilot_case", "case_id"),
        Index("ix_ai_case_copilot_status", "status"),
        Index("ix_ai_case_copilot_created", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"), nullable=False)
    request_key: Mapped[str] = mapped_column(String(256), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_role: Mapped[str] = mapped_column(String(32), nullable=False)
    question_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    proposal_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    grounding_report_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    routed_control_intent: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False, default="ai-case-copilot-v1")
    model_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AIDiagnosticCycle(Base):
    __tablename__ = "ai_diagnostic_cycles"
    __table_args__ = (
        UniqueConstraint("case_id", "cycle_no", name="uq_ai_diagnostic_cycle_case_no"),
        UniqueConstraint(
            "case_id", "snapshot_fingerprint", "runtime_stage",
            name="uq_ai_diagnostic_cycle_snapshot_stage",
        ),
        CheckConstraint("runtime_stage IN ('SHADOW','SUGGEST')", name="ck_ai_diagnostic_cycle_stage"),
        CheckConstraint(
            "status IN ('COMPLETED','DEGRADED','STOPPED','REQUIRE_HUMAN')",
            name="ck_ai_diagnostic_cycle_status",
        ),
        CheckConstraint(
            "continue_recommendation IN ('CONTINUE','STOP','REQUIRE_HUMAN')",
            name="ck_ai_diagnostic_cycle_continue",
        ),
        Index("ix_ai_diagnostic_cycle_case", "case_id"),
        Index("ix_ai_diagnostic_cycle_stage", "runtime_stage"),
        Index("ix_ai_diagnostic_cycle_status", "status"),
        Index("ix_ai_diagnostic_cycle_created", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"), nullable=False)
    cycle_no: Mapped[int] = mapped_column(Integer, nullable=False)
    runtime_stage: Mapped[str] = mapped_column(String(24), nullable=False)
    snapshot_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    proposal_id: Mapped[str | None] = mapped_column(ForeignKey("ai_proposals.id", ondelete="SET NULL"), nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    known_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    unknown_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    excluded_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    hypotheses_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    critic_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    next_action_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    selection_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    continue_recommendation: Mapped[str] = mapped_column(String(24), nullable=False)
    stop_reason: Mapped[str | None] = mapped_column(String(128), nullable=True)
    no_progress_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    formal_result_changed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    dispatch_attempted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    dispatch_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
