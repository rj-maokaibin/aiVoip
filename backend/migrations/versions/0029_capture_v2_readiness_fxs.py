"""Capture Engine V2.1 readiness and FXS semantics
Revision ID: 0029_capture_v2_readiness_fxs
Revises: 0028_capture_v2_reliable_segments
Create Date: 2026-08-20
"""
from alembic import op
import sqlalchemy as sa

revision = "0029_capture_v2_readiness_fxs"
down_revision = "0028_capture_v2_reliable_segments"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "readiness_snapshots",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("capture_session_id", sa.String(36), sa.ForeignKey("capture_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("stage", sa.String(40), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("checks", sa.JSON(), nullable=False),
        sa.Column("reasons", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("schema_version", sa.String(64), nullable=False, server_default="readiness-v2.1"),
    )
    op.create_index("ix_readiness_session_stage", "readiness_snapshots", ["capture_session_id", "stage", "created_at"])
    op.create_table(
        "capture_attempts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("capture_session_id", sa.String(36), sa.ForeignKey("capture_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("reproduction_attempt_id", sa.String(36), nullable=True),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("candidate_start_source_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmed_start_source_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_source_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmation_source", sa.String(64), nullable=True),
        sa.Column("classification", sa.String(64), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("capture_session_id", "attempt_no", name="uq_capture_attempt_session_no"),
    )
    op.create_index("ix_capture_attempt_session_state", "capture_attempts", ["capture_session_id", "state"])
    op.create_index("ix_capture_attempt_repro", "capture_attempts", ["reproduction_attempt_id"])
    op.create_table(
        "attempt_data_plane_verifications",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("capture_attempt_id", sa.String(36), sa.ForeignKey("capture_attempts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("channel", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="PENDING"),
        sa.Column("expectation_created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("verification_deadline", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_seen_source_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason_code", sa.String(128), nullable=True),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("capture_attempt_id", "channel", name="uq_attempt_data_plane_channel"),
    )
    op.create_index("ix_attempt_dp_status", "attempt_data_plane_verifications", ["capture_attempt_id", "status"])
    op.create_index("ix_attempt_dp_deadline", "attempt_data_plane_verifications", ["verification_deadline"])


def downgrade():
    op.drop_index("ix_attempt_dp_deadline", table_name="attempt_data_plane_verifications")
    op.drop_index("ix_attempt_dp_status", table_name="attempt_data_plane_verifications")
    op.drop_table("attempt_data_plane_verifications")
    op.drop_index("ix_capture_attempt_repro", table_name="capture_attempts")
    op.drop_index("ix_capture_attempt_session_state", table_name="capture_attempts")
    op.drop_table("capture_attempts")
    op.drop_index("ix_readiness_session_stage", table_name="readiness_snapshots")
    op.drop_table("readiness_snapshots")
