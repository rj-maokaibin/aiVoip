"""Capture Engine V2.1 quality and evidence report
Revision ID: 0031_capture_v2_quality_report
Revises: 0030_capture_v2_coverage
Create Date: 2026-08-20
"""
from alembic import op
import sqlalchemy as sa

revision = "0031_capture_v2_quality_report"
down_revision = "0030_capture_v2_coverage"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "quality_snapshots",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("idempotency_key", sa.String(160), nullable=False, unique=True),
        sa.Column("coverage_window_id", sa.String(36), sa.ForeignKey("coverage_windows.id", ondelete="SET NULL"), nullable=True),
        sa.Column("capture_session_id", sa.String(36), sa.ForeignKey("capture_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("capture_attempt_id", sa.String(36), sa.ForeignKey("capture_attempts.id", ondelete="SET NULL"), nullable=True),
        sa.Column("call_ref", sa.String(128), nullable=True),
        sa.Column("capture_completeness", sa.String(24), nullable=False),
        sa.Column("diagnostic_confidence", sa.String(24), nullable=False),
        sa.Column("policy_version", sa.String(64), nullable=False),
        sa.Column("reasons", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_quality_session", "quality_snapshots", ["capture_session_id", "created_at"])
    op.create_index("ix_quality_idempotency", "quality_snapshots", ["idempotency_key"], unique=True)
    op.create_index("ix_quality_attempt", "quality_snapshots", ["capture_attempt_id"])
    op.create_index("ix_quality_coverage_window", "quality_snapshots", ["coverage_window_id"])

    op.create_table(
        "signal_availability",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("quality_snapshot_id", sa.String(36), sa.ForeignKey("quality_snapshots.id", ondelete="CASCADE"), nullable=False),
        sa.Column("channel", sa.String(32), nullable=False),
        sa.Column("availability", sa.String(40), nullable=False),
        sa.Column("reason_code", sa.String(128), nullable=True),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.UniqueConstraint("quality_snapshot_id", "channel", name="uq_quality_signal_channel"),
    )
    op.create_index("ix_signal_availability", "signal_availability", ["channel", "availability"])

    op.create_table(
        "evidence_assets",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("idempotency_key", sa.String(160), nullable=True, unique=True),
        sa.Column("capture_session_id", sa.String(36), sa.ForeignKey("capture_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("capture_attempt_id", sa.String(36), sa.ForeignKey("capture_attempts.id", ondelete="SET NULL"), nullable=True),
        sa.Column("call_ref", sa.String(128), nullable=True),
        sa.Column("asset_type", sa.String(40), nullable=False),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("storage_key", sa.Text(), nullable=True),
        sa.Column("source_refs", sa.JSON(), nullable=False),
        sa.Column("start_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_evidence_asset_session_type", "evidence_assets", ["capture_session_id", "asset_type"])
    op.create_index("ix_evidence_asset_idempotency", "evidence_assets", ["idempotency_key"], unique=True)
    op.create_index("ix_evidence_asset_attempt", "evidence_assets", ["capture_attempt_id"])
    op.create_index("ix_evidence_asset_call", "evidence_assets", ["call_ref"])


def downgrade():
    op.drop_index("ix_evidence_asset_call", table_name="evidence_assets")
    op.drop_index("ix_evidence_asset_idempotency", table_name="evidence_assets")
    op.drop_index("ix_evidence_asset_attempt", table_name="evidence_assets")
    op.drop_index("ix_evidence_asset_session_type", table_name="evidence_assets")
    op.drop_table("evidence_assets")
    op.drop_index("ix_signal_availability", table_name="signal_availability")
    op.drop_table("signal_availability")
    op.drop_index("ix_quality_attempt", table_name="quality_snapshots")
    op.drop_index("ix_quality_coverage_window", table_name="quality_snapshots")
    op.drop_index("ix_quality_idempotency", table_name="quality_snapshots")
    op.drop_index("ix_quality_session", table_name="quality_snapshots")
    op.drop_table("quality_snapshots")
