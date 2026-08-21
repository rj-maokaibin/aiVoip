"""Capture Engine V2.1 foundation + ownership

Revision ID: 0027_capture_v2_foundation
Revises: 0026_ai_diagnostic_loop_v1
Create Date: 2026-08-20
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0027_capture_v2_foundation"
down_revision: Union[str, None] = "0026_ai_diagnostic_loop_v1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "capture_sessions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("reproduction_session_id", sa.String(36), sa.ForeignKey("reproduction_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("device_id", sa.String(36), sa.ForeignKey("case_devices.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("state", sa.String(40), nullable=False, server_default="CREATED"),
        sa.Column("health_status", sa.String(24), nullable=False, server_default="HEALTHY"),
        sa.Column("capture_profile_id", sa.String(128), nullable=False),
        sa.Column("capture_profile_version", sa.String(64), nullable=False),
        sa.Column("platform_profile_id", sa.String(64), nullable=False),
        sa.Column("platform_profile_version", sa.String(64), nullable=False),
        sa.Column("effective_profile", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("path_ready_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("target_confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("evidence_durable_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_code", sa.String(128), nullable=True),
        sa.Column("cleanup_status", sa.String(32), nullable=True),
        sa.Column("schema_version", sa.String(64), nullable=False, server_default="capture-v2.1.1"),
        sa.UniqueConstraint("reproduction_session_id", name="uq_capture_session_reproduction"),
        sa.CheckConstraint(
            "state IN ('CREATED','ACQUIRING_LEASE','RECOVERING','PREPARING','CAPTURE_PATH_READY','WATCHING','TARGET_CONFIRMED','POST_TARGET_OBSERVATION','EVIDENCE_DRAINING','COVERAGE_FINALIZING','CLEANUP','COMPLETED','FAILED')",
            name="ck_capture_session_state",
        ),
        sa.CheckConstraint("health_status IN ('HEALTHY','DEGRADED','FAILED')", name="ck_capture_session_health"),
    )
    op.create_index("ix_capture_session_reproduction", "capture_sessions", ["reproduction_session_id"])
    op.create_index("ix_capture_session_device_state", "capture_sessions", ["device_id", "state"])

    op.create_table(
        "capture_leases",
        sa.Column("device_id", sa.String(36), sa.ForeignKey("case_devices.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("capture_session_id", sa.String(36), sa.ForeignKey("capture_sessions.id", ondelete="SET NULL"), nullable=True),
        sa.Column("owner_worker_id", sa.String(128), nullable=True),
        sa.Column("lease_epoch", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("state", sa.String(24), nullable=False, server_default="RELEASED"),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("renewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        sa.CheckConstraint("lease_epoch >= 0", name="ck_capture_lease_epoch_nonnegative"),
        sa.CheckConstraint("state IN ('ACTIVE','EXPIRED','FENCED','RELEASING','RELEASED')", name="ck_capture_lease_state"),
    )
    op.create_index("ix_capture_lease_session", "capture_leases", ["capture_session_id"])
    op.create_index("ix_capture_lease_state_expiry", "capture_leases", ["state", "expires_at"])

    op.create_table(
        "capture_epochs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("capture_session_id", sa.String(36), sa.ForeignKey("capture_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("device_id", sa.String(36), sa.ForeignKey("case_devices.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("epoch_index", sa.BigInteger(), nullable=False),
        sa.Column("epoch_token", sa.String(128), nullable=False),
        sa.Column("boot_id", sa.String(128), nullable=True),
        sa.Column("producer_pid", sa.Integer(), nullable=True),
        sa.Column("producer_starttime", sa.BigInteger(), nullable=True),
        sa.Column("producer_cmdline", sa.Text(), nullable=True),
        sa.Column("interface", sa.String(128), nullable=True),
        sa.Column("capture_mode", sa.String(32), nullable=False, server_default="FULL_VOICE"),
        sa.Column("lease_epoch_started", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.String(24), nullable=False, server_default="STARTING"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_reason", sa.String(128), nullable=True),
        sa.Column("packets_captured", sa.BigInteger(), nullable=True),
        sa.Column("packets_received", sa.BigInteger(), nullable=True),
        sa.Column("packets_dropped_kernel", sa.BigInteger(), nullable=True),
        sa.UniqueConstraint("device_id", "epoch_token", name="uq_capture_epoch_device_token"),
        sa.UniqueConstraint("capture_session_id", "epoch_index", name="uq_capture_epoch_session_index"),
        sa.CheckConstraint("state IN ('STARTING','RUNNING','ENDED','FAILED')", name="ck_capture_epoch_state"),
        sa.CheckConstraint("lease_epoch_started > 0", name="ck_capture_epoch_lease_positive"),
    )
    op.create_index("ix_capture_epoch_session_state", "capture_epochs", ["capture_session_id", "state"])
    op.create_index("ix_capture_epoch_device_state", "capture_epochs", ["device_id", "state"])

    op.create_table(
        "capture_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("capture_session_id", sa.String(36), sa.ForeignKey("capture_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("entity_type", sa.String(64), nullable=False),
        sa.Column("entity_id", sa.String(128), nullable=True),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("source_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("schema_version", sa.String(64), nullable=False, server_default="capture-event-v2.1"),
    )
    op.create_index("ix_capture_event_session_recorded", "capture_events", ["capture_session_id", "recorded_at"])
    op.create_index("ix_capture_event_type", "capture_events", ["event_type"])
    op.create_index("ix_capture_event_entity", "capture_events", ["entity_type", "entity_id"])

    op.create_table(
        "capture_gaps",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("capture_session_id", sa.String(36), sa.ForeignKey("capture_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("capture_epoch_id", sa.String(36), sa.ForeignKey("capture_epochs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("channel", sa.String(32), nullable=False),
        sa.Column("gap_start_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("gap_end_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("certainty", sa.String(16), nullable=False),
        sa.Column("reason_code", sa.String(128), nullable=False),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("recovered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("details", sa.JSON(), nullable=True),
        sa.CheckConstraint("certainty IN ('CONFIRMED','POSSIBLE')", name="ck_capture_gap_certainty"),
    )
    op.create_index("ix_capture_gap_session_channel", "capture_gaps", ["capture_session_id", "channel"])
    op.create_index("ix_capture_gap_reason", "capture_gaps", ["reason_code"])


def downgrade() -> None:
    op.drop_index("ix_capture_gap_reason", table_name="capture_gaps")
    op.drop_index("ix_capture_gap_session_channel", table_name="capture_gaps")
    op.drop_table("capture_gaps")

    op.drop_index("ix_capture_event_entity", table_name="capture_events")
    op.drop_index("ix_capture_event_type", table_name="capture_events")
    op.drop_index("ix_capture_event_session_recorded", table_name="capture_events")
    op.drop_table("capture_events")

    op.drop_index("ix_capture_epoch_device_state", table_name="capture_epochs")
    op.drop_index("ix_capture_epoch_session_state", table_name="capture_epochs")
    op.drop_table("capture_epochs")

    op.drop_index("ix_capture_lease_state_expiry", table_name="capture_leases")
    op.drop_index("ix_capture_lease_session", table_name="capture_leases")
    op.drop_table("capture_leases")

    op.drop_index("ix_capture_session_device_state", table_name="capture_sessions")
    op.drop_index("ix_capture_session_reproduction", table_name="capture_sessions")
    op.drop_table("capture_sessions")
