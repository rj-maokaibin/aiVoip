"""Capture Engine V2.1 coverage ledger
Revision ID: 0030_capture_v2_coverage
Revises: 0029_capture_v2_readiness_fxs
Create Date: 2026-08-20
"""
from alembic import op
import sqlalchemy as sa

revision = "0030_capture_v2_coverage"
down_revision = "0029_capture_v2_readiness_fxs"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "coverage_windows",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("idempotency_key", sa.String(160), nullable=False, unique=True),
        sa.Column("capture_session_id", sa.String(36), sa.ForeignKey("capture_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("capture_attempt_id", sa.String(36), sa.ForeignKey("capture_attempts.id", ondelete="SET NULL"), nullable=True),
        sa.Column("call_ref", sa.String(128), nullable=True),
        sa.Column("window_type", sa.String(40), nullable=False),
        sa.Column("required_start_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("required_end_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="PENDING"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.CheckConstraint("required_end_ts > required_start_ts", name="ck_coverage_window_positive"),
    )
    op.create_index("ix_coverage_window_session", "coverage_windows", ["capture_session_id", "status"])
    op.create_index("ix_coverage_window_idempotency", "coverage_windows", ["idempotency_key"], unique=True)
    op.create_index("ix_coverage_window_attempt", "coverage_windows", ["capture_attempt_id"])

    op.create_table(
        "coverage_tracks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("coverage_window_id", sa.String(36), sa.ForeignKey("coverage_windows.id", ondelete="CASCADE"), nullable=False),
        sa.Column("channel", sa.String(32), nullable=False),
        sa.Column("requirement", sa.String(32), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="PENDING"),
        sa.Column("required_ms", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("covered_ms", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("gap_ms", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("unknown_ms", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.UniqueConstraint("coverage_window_id", "channel", name="uq_coverage_window_channel"),
    )
    op.create_index("ix_coverage_track_status", "coverage_tracks", ["coverage_window_id", "status"])

    op.create_table(
        "coverage_intervals",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("coverage_track_id", sa.String(36), sa.ForeignKey("coverage_tracks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("interval_start_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("interval_end_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("interval_type", sa.String(24), nullable=False),
        sa.Column("source_kind", sa.String(32), nullable=False),
        sa.Column("source_id", sa.String(128), nullable=True),
        sa.Column("certainty", sa.String(16), nullable=False, server_default="CONFIRMED"),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.CheckConstraint("interval_end_ts > interval_start_ts", name="ck_coverage_interval_positive"),
    )
    op.create_index("ix_coverage_interval_track", "coverage_intervals", ["coverage_track_id", "interval_start_ts"])
    op.create_index("ix_coverage_interval_source", "coverage_intervals", ["source_kind", "source_id"])


def downgrade():
    op.drop_index("ix_coverage_interval_source", table_name="coverage_intervals")
    op.drop_index("ix_coverage_interval_track", table_name="coverage_intervals")
    op.drop_table("coverage_intervals")
    op.drop_index("ix_coverage_track_status", table_name="coverage_tracks")
    op.drop_table("coverage_tracks")
    op.drop_index("ix_coverage_window_attempt", table_name="coverage_windows")
    op.drop_index("ix_coverage_window_idempotency", table_name="coverage_windows")
    op.drop_index("ix_coverage_window_session", table_name="coverage_windows")
    op.drop_table("coverage_windows")
