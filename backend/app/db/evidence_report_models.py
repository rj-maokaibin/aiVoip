from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.ids import new_id
from app.db.base import Base
from app.contracts.evidence_report import EvidenceFindingStatus, EvidenceFindingSeverity, EvidenceReportStatus


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PreliminaryEvidenceReport(Base):
    __tablename__ = "preliminary_evidence_reports"
    __table_args__ = (
        UniqueConstraint("scope_type", "scope_id", "version", name="uq_preliminary_evidence_report_scope_version"),
        UniqueConstraint("idempotency_key", name="uq_preliminary_evidence_report_idempotency"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"), index=True)
    session_id: Mapped[str | None] = mapped_column(ForeignKey("reproduction_sessions.id", ondelete="CASCADE"), nullable=True, index=True)
    call_id: Mapped[str | None] = mapped_column(ForeignKey("reproduction_calls.id", ondelete="CASCADE"), nullable=True, index=True)
    scope_type: Mapped[str] = mapped_column(String(16), index=True)
    scope_id: Mapped[str] = mapped_column(String(36), index=True)
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), index=True, default=EvidenceReportStatus.PENDING.value)
    schema_version: Mapped[str] = mapped_column(String(64))
    composer_version: Mapped[str] = mapped_column(String(64))
    input_snapshot_hash: Mapped[str] = mapped_column(String(64), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(64), index=True)
    analyzer_versions_json: Mapped[dict] = mapped_column(JSON, default=dict)
    environment_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    environment_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    completeness_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    boundary_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    snapshot_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    json_object_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    html_object_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    manifest_object_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    bundle_object_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    supersedes_report_id: Mapped[str | None] = mapped_column(ForeignKey("preliminary_evidence_reports.id", ondelete="SET NULL"), nullable=True, index=True)
    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EvidenceFinding(Base):
    __tablename__ = "evidence_findings"
    __table_args__ = (
        UniqueConstraint("scope_type", "scope_id", "stable_key", name="uq_evidence_finding_scope_stable_key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"), index=True)
    session_id: Mapped[str | None] = mapped_column(ForeignKey("reproduction_sessions.id", ondelete="CASCADE"), nullable=True, index=True)
    call_id: Mapped[str | None] = mapped_column(ForeignKey("reproduction_calls.id", ondelete="CASCADE"), nullable=True, index=True)
    scope_type: Mapped[str] = mapped_column(String(16), index=True)
    scope_id: Mapped[str] = mapped_column(String(36), index=True)
    stable_key: Mapped[str] = mapped_column(String(128), index=True)
    finding_signature: Mapped[str] = mapped_column(String(512), index=True)
    signature_version: Mapped[str] = mapped_column(String(32))
    finding_type: Mapped[str] = mapped_column(String(128), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True, default=EvidenceFindingStatus.PROPOSED.value)
    severity: Mapped[str] = mapped_column(String(16), index=True, default=EvidenceFindingSeverity.INFO.value)
    evidence_level: Mapped[str] = mapped_column(String(8), index=True, default="L3")
    title: Mapped[str] = mapped_column(String(512))
    observation: Mapped[str] = mapped_column(Text)
    interpretation: Mapped[str | None] = mapped_column(Text, nullable=True)
    root_cause_boundary: Mapped[str] = mapped_column(Text)
    start_time: Mapped[float | None] = mapped_column(nullable=True)
    end_time: Mapped[float | None] = mapped_column(nullable=True)
    representative_time: Mapped[float | None] = mapped_column(nullable=True)
    scope_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    metrics_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    evidence_refs_json: Mapped[list] = mapped_column(JSON, default=list)
    artifact_refs_json: Mapped[list] = mapped_column(JSON, default=list)
    event_refs_json: Mapped[list] = mapped_column(JSON, default=list)
    correlation_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    source_analyzer_run_ids: Mapped[list] = mapped_column(JSON, default=list)
    occurrence_count: Mapped[int] = mapped_column(Integer, default=1)
    first_seen_report_version: Mapped[int] = mapped_column(Integer, default=1)
    last_seen_report_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class EvidenceReportArtifactLink(Base):
    __tablename__ = "evidence_report_artifact_links"
    __table_args__ = (
        UniqueConstraint("report_id", "artifact_id", name="uq_evidence_report_artifact_link"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    report_id: Mapped[str] = mapped_column(ForeignKey("preliminary_evidence_reports.id", ondelete="CASCADE"), index=True)
    artifact_id: Mapped[str] = mapped_column(ForeignKey("artifacts.id", ondelete="CASCADE"), index=True)
    finding_ids_json: Mapped[list] = mapped_column(JSON, default=list)
    role: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class FeishuEvidenceDocumentBinding(Base):
    __tablename__ = "feishu_evidence_document_bindings"
    __table_args__ = (UniqueConstraint("case_id", name="uq_feishu_evidence_document_case"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"), index=True)
    document_id: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)
    document_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="PENDING", index=True)
    projected_report_id: Mapped[str | None] = mapped_column(ForeignKey("preliminary_evidence_reports.id", ondelete="SET NULL"), nullable=True, index=True)
    projection_version: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
