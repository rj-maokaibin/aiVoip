"""Preliminary Evidence Report V1 persistence

Revision ID: 0019_preliminary_evidence_report_v1
Revises: 0018_golden_candidate_assessments
Create Date: 2026-08-18
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0019_preliminary_evidence_report_v1"
down_revision: Union[str, None] = "0018_golden_candidate_assessments"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "preliminary_evidence_reports",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("case_id", sa.String(36), nullable=False),
        sa.Column("session_id", sa.String(36), nullable=True),
        sa.Column("call_id", sa.String(36), nullable=True),
        sa.Column("scope_type", sa.String(16), nullable=False),
        sa.Column("scope_id", sa.String(36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("schema_version", sa.String(64), nullable=False),
        sa.Column("composer_version", sa.String(64), nullable=False),
        sa.Column("input_snapshot_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(64), nullable=False),
        sa.Column("analyzer_versions_json", sa.JSON(), nullable=False),
        sa.Column("environment_fingerprint", sa.String(64), nullable=True),
        sa.Column("environment_json", sa.JSON(), nullable=True),
        sa.Column("completeness_json", sa.JSON(), nullable=True),
        sa.Column("boundary_json", sa.JSON(), nullable=True),
        sa.Column("snapshot_json", sa.JSON(), nullable=True),
        sa.Column("json_object_key", sa.String(1024), nullable=True),
        sa.Column("html_object_key", sa.String(1024), nullable=True),
        sa.Column("manifest_object_key", sa.String(1024), nullable=True),
        sa.Column("bundle_object_key", sa.String(1024), nullable=True),
        sa.Column("supersedes_report_id", sa.String(36), nullable=True),
        sa.Column("created_by", sa.String(128), nullable=True),
        sa.Column("error_code", sa.String(128), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["session_id"], ["reproduction_sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["call_id"], ["reproduction_calls.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["supersedes_report_id"], ["preliminary_evidence_reports.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("scope_type", "scope_id", "version", name="uq_preliminary_evidence_report_scope_version"),
        sa.UniqueConstraint("idempotency_key", name="uq_preliminary_evidence_report_idempotency"),
    )
    for name, cols in [
        ("ix_preliminary_evidence_reports_case_id", ["case_id"]),
        ("ix_preliminary_evidence_reports_session_id", ["session_id"]),
        ("ix_preliminary_evidence_reports_call_id", ["call_id"]),
        ("ix_preliminary_evidence_reports_scope_type", ["scope_type"]),
        ("ix_preliminary_evidence_reports_scope_id", ["scope_id"]),
        ("ix_preliminary_evidence_reports_status", ["status"]),
        ("ix_preliminary_evidence_reports_input_snapshot_hash", ["input_snapshot_hash"]),
        ("ix_preliminary_evidence_reports_idempotency_key", ["idempotency_key"]),
        ("ix_preliminary_evidence_reports_environment_fingerprint", ["environment_fingerprint"]),
        ("ix_preliminary_evidence_reports_supersedes_report_id", ["supersedes_report_id"]),
    ]:
        op.create_index(name, "preliminary_evidence_reports", cols)

    op.create_table(
        "evidence_findings",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("case_id", sa.String(36), nullable=False),
        sa.Column("session_id", sa.String(36), nullable=True),
        sa.Column("call_id", sa.String(36), nullable=True),
        sa.Column("scope_type", sa.String(16), nullable=False),
        sa.Column("scope_id", sa.String(36), nullable=False),
        sa.Column("stable_key", sa.String(128), nullable=False),
        sa.Column("finding_signature", sa.String(512), nullable=False),
        sa.Column("signature_version", sa.String(32), nullable=False),
        sa.Column("finding_type", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("evidence_level", sa.String(8), nullable=False),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("observation", sa.Text(), nullable=False),
        sa.Column("interpretation", sa.Text(), nullable=True),
        sa.Column("root_cause_boundary", sa.Text(), nullable=False),
        sa.Column("start_time", sa.Float(), nullable=True),
        sa.Column("end_time", sa.Float(), nullable=True),
        sa.Column("representative_time", sa.Float(), nullable=True),
        sa.Column("scope_json", sa.JSON(), nullable=True),
        sa.Column("metrics_json", sa.JSON(), nullable=True),
        sa.Column("evidence_refs_json", sa.JSON(), nullable=False),
        sa.Column("artifact_refs_json", sa.JSON(), nullable=False),
        sa.Column("event_refs_json", sa.JSON(), nullable=False),
        sa.Column("correlation_json", sa.JSON(), nullable=True),
        sa.Column("source_analyzer_run_ids", sa.JSON(), nullable=False),
        sa.Column("occurrence_count", sa.Integer(), nullable=False),
        sa.Column("first_seen_report_version", sa.Integer(), nullable=False),
        sa.Column("last_seen_report_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["session_id"], ["reproduction_sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["call_id"], ["reproduction_calls.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("scope_type", "scope_id", "stable_key", name="uq_evidence_finding_scope_stable_key"),
    )
    for name, cols in [
        ("ix_evidence_findings_case_id", ["case_id"]),
        ("ix_evidence_findings_session_id", ["session_id"]),
        ("ix_evidence_findings_call_id", ["call_id"]),
        ("ix_evidence_findings_scope_type", ["scope_type"]),
        ("ix_evidence_findings_scope_id", ["scope_id"]),
        ("ix_evidence_findings_stable_key", ["stable_key"]),
        ("ix_evidence_findings_finding_signature", ["finding_signature"]),
        ("ix_evidence_findings_finding_type", ["finding_type"]),
        ("ix_evidence_findings_status", ["status"]),
        ("ix_evidence_findings_severity", ["severity"]),
        ("ix_evidence_findings_evidence_level", ["evidence_level"]),
    ]:
        op.create_index(name, "evidence_findings", cols)

    op.create_table(
        "evidence_report_artifact_links",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("report_id", sa.String(36), nullable=False),
        sa.Column("artifact_id", sa.String(36), nullable=False),
        sa.Column("finding_ids_json", sa.JSON(), nullable=False),
        sa.Column("role", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["report_id"], ["preliminary_evidence_reports.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["artifact_id"], ["artifacts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("report_id", "artifact_id", name="uq_evidence_report_artifact_link"),
    )
    op.create_index("ix_evidence_report_artifact_links_report_id", "evidence_report_artifact_links", ["report_id"])
    op.create_index("ix_evidence_report_artifact_links_artifact_id", "evidence_report_artifact_links", ["artifact_id"])
    op.create_index("ix_evidence_report_artifact_links_role", "evidence_report_artifact_links", ["role"])

    op.create_table(
        "feishu_evidence_document_bindings",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("case_id", sa.String(36), nullable=False),
        sa.Column("document_id", sa.String(256), nullable=True),
        sa.Column("document_url", sa.Text(), nullable=True),
        sa.Column("title", sa.String(512), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("projected_report_id", sa.String(36), nullable=True),
        sa.Column("projection_version", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["projected_report_id"], ["preliminary_evidence_reports.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("case_id", name="uq_feishu_evidence_document_case"),
    )
    op.create_index("ix_feishu_evidence_document_bindings_case_id", "feishu_evidence_document_bindings", ["case_id"])
    op.create_index("ix_feishu_evidence_document_bindings_document_id", "feishu_evidence_document_bindings", ["document_id"])
    op.create_index("ix_feishu_evidence_document_bindings_status", "feishu_evidence_document_bindings", ["status"])
    op.create_index("ix_feishu_evidence_document_bindings_projected_report_id", "feishu_evidence_document_bindings", ["projected_report_id"])


def downgrade() -> None:
    op.drop_table("feishu_evidence_document_bindings")
    op.drop_table("evidence_report_artifact_links")
    op.drop_table("evidence_findings")
    op.drop_table("preliminary_evidence_reports")
