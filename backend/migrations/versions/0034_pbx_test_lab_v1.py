"""FusionPBX / FreeSWITCH Test Lab Infrastructure V1

Revision ID: 0034_pbx_test_lab_v1
Revises: 0033_automation_test_runtime_v1
Create Date: 2026-09-09
"""
from alembic import op
import sqlalchemy as sa

revision = "0034_pbx_test_lab_v1"
down_revision = "0033_automation_test_runtime_v1"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "pbx_nodes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("node_key", sa.String(128), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("runtime_provider", sa.String(32), nullable=False),
        sa.Column("host", sa.String(255), nullable=False),
        sa.Column("fusionpbx_root", sa.String(512), nullable=False),
        sa.Column("internal_profile", sa.String(64), nullable=False),
        sa.Column("internal_port", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("source_fence_version", sa.String(128), nullable=False),
        sa.Column("provider_metadata_json", sa.JSON(), nullable=True),
        sa.Column("last_health_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_pbx_node_key", "pbx_nodes", ["node_key"], unique=True)
    op.create_index("ix_pbx_node_status", "pbx_nodes", ["status", "last_health_at"])

    op.create_table(
        "pbx_extension_resources",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("pbx_node_id", sa.String(36), sa.ForeignKey("pbx_nodes.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("domain_id", sa.String(128), nullable=False),
        sa.Column("extension", sa.String(128), nullable=False),
        sa.Column("resource_type", sa.String(32), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("lease_id", sa.String(36), nullable=True),
        sa.Column("lease_epoch", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("owner_run_id", sa.String(128), nullable=True),
        sa.Column("created_by_automation", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("deletable", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("ownership_marker", sa.String(255), nullable=True),
        sa.Column("last_health_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_cleanup_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dirty_reason", sa.Text(), nullable=True),
        sa.Column("provider_metadata_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("pbx_node_id", "domain_id", "extension", name="uq_pbx_extension_identity"),
    )
    op.create_index("ix_pbx_extension_state", "pbx_extension_resources", ["pbx_node_id", "state"])
    op.create_index("ix_pbx_extension_owner", "pbx_extension_resources", ["owner_run_id", "lease_epoch"])
    op.create_index("ix_pbx_extension_identity", "pbx_extension_resources", ["domain_id", "extension"])

    op.create_table(
        "pbx_resource_leases",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("pbx_node_id", sa.String(36), sa.ForeignKey("pbx_nodes.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("resource_id", sa.String(36), sa.ForeignKey("pbx_extension_resources.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("run_id", sa.String(128), nullable=False),
        sa.Column("owner_worker_id", sa.String(128), nullable=False),
        sa.Column("lease_epoch", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("renewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_pbx_lease_resource_state", "pbx_resource_leases", ["resource_id", "state", "expires_at"])
    op.create_index("ix_pbx_lease_run", "pbx_resource_leases", ["run_id", "state"])

    op.create_table(
        "pbx_mutation_snapshots",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("resource_id", sa.String(36), sa.ForeignKey("pbx_extension_resources.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("lease_id", sa.String(36), sa.ForeignKey("pbx_resource_leases.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("run_id", sa.String(128), nullable=False),
        sa.Column("mutation_kind", sa.String(64), nullable=False),
        sa.Column("snapshot_json", sa.JSON(), nullable=False),
        sa.Column("schema_version", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_pbx_snapshot_resource_run", "pbx_mutation_snapshots", ["resource_id", "run_id", "created_at"])

    op.create_table(
        "pbx_call_sessions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(128), nullable=False),
        sa.Column("pbx_node_id", sa.String(36), sa.ForeignKey("pbx_nodes.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("freeswitch_uuid", sa.String(128), nullable=True, unique=True),
        sa.Column("caller_resource_id", sa.String(36), sa.ForeignKey("pbx_extension_resources.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("callee_resource_id", sa.String(36), sa.ForeignKey("pbx_extension_resources.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("direction", sa.String(32), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("ringing_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("hangup_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("hangup_cause", sa.String(128), nullable=True),
        sa.Column("sip_evidence_refs_json", sa.JSON(), nullable=False),
        sa.Column("rtp_evidence_refs_json", sa.JSON(), nullable=False),
        sa.Column("dtmf_evidence_refs_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_pbx_call_run_state", "pbx_call_sessions", ["run_id", "state"])
    op.create_index("ix_pbx_call_node_state", "pbx_call_sessions", ["pbx_node_id", "state"])


def downgrade():
    op.drop_index("ix_pbx_call_node_state", table_name="pbx_call_sessions")
    op.drop_index("ix_pbx_call_run_state", table_name="pbx_call_sessions")
    op.drop_table("pbx_call_sessions")
    op.drop_index("ix_pbx_snapshot_resource_run", table_name="pbx_mutation_snapshots")
    op.drop_table("pbx_mutation_snapshots")
    op.drop_index("ix_pbx_lease_run", table_name="pbx_resource_leases")
    op.drop_index("ix_pbx_lease_resource_state", table_name="pbx_resource_leases")
    op.drop_table("pbx_resource_leases")
    op.drop_index("ix_pbx_extension_identity", table_name="pbx_extension_resources")
    op.drop_index("ix_pbx_extension_owner", table_name="pbx_extension_resources")
    op.drop_index("ix_pbx_extension_state", table_name="pbx_extension_resources")
    op.drop_table("pbx_extension_resources")
    op.drop_index("ix_pbx_node_status", table_name="pbx_nodes")
    op.drop_index("ix_pbx_node_key", table_name="pbx_nodes")
    op.drop_table("pbx_nodes")
