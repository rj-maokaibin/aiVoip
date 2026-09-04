"""Generic VOIP Automation Framework V1 test runtime

Revision ID: 0033_automation_test_runtime_v1
Revises: 0032_conversation_knowledge_v1
Create Date: 2026-09-05
"""
from alembic import op
import sqlalchemy as sa

revision = "0033_automation_test_runtime_v1"
down_revision = "0032_conversation_knowledge_v1"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "automation_test_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("case_id", sa.String(36), sa.ForeignKey("cases.id", ondelete="CASCADE"), nullable=False),
        sa.Column("suite_id", sa.String(128), nullable=False),
        sa.Column("case_key", sa.String(128), nullable=False),
        sa.Column("case_version", sa.Integer(), nullable=False),
        sa.Column("case_checksum", sa.String(64), nullable=True),
        sa.Column("intent", sa.String(16), nullable=False, server_default="verify"),
        sa.Column("status", sa.String(32), nullable=False, server_default="CREATED"),
        sa.Column("environment_profile", sa.String(128), nullable=False),
        sa.Column("effective_plan_json", sa.JSON(), nullable=False),
        sa.Column("worker_id", sa.String(128), nullable=True),
        sa.Column("authority_ref", sa.JSON(), nullable=True),
        sa.Column("verdict", sa.String(32), nullable=True),
        sa.Column("terminal_reason", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_automation_test_run_case", "automation_test_runs", ["case_id", "case_key"])
    op.create_index("ix_automation_test_run_status", "automation_test_runs", ["status", "created_at"])
    op.create_index("ix_automation_test_run_worker", "automation_test_runs", ["worker_id"])
    op.create_index("ix_automation_test_run_checksum", "automation_test_runs", ["case_checksum"])

    op.create_table(
        "automation_test_step_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("test_run_id", sa.String(36), sa.ForeignKey("automation_test_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("step_no", sa.Integer(), nullable=False),
        sa.Column("action_id", sa.String(160), nullable=False),
        sa.Column("route_json", sa.JSON(), nullable=True),
        sa.Column("purpose", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("action_run_id", sa.String(36), sa.ForeignKey("action_runs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("output_json", sa.JSON(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("test_run_id", "step_no", name="uq_automation_test_step_no"),
    )
    op.create_index("ix_automation_test_step_run", "automation_test_step_runs", ["test_run_id", "purpose", "status"])
    op.create_index("ix_automation_test_step_action", "automation_test_step_runs", ["action_id"])

    op.create_table(
        "assertion_results",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("test_run_id", sa.String(36), sa.ForeignKey("automation_test_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("assertion_no", sa.Integer(), nullable=False),
        sa.Column("assertion_id", sa.String(128), nullable=False),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("path", sa.String(512), nullable=False),
        sa.Column("operator", sa.String(32), nullable=False),
        sa.Column("expected_json", sa.JSON(), nullable=True),
        sa.Column("actual_json", sa.JSON(), nullable=True),
        sa.Column("verdict", sa.String(32), nullable=False),
        sa.Column("evidence_refs_json", sa.JSON(), nullable=False),
        sa.Column("route_json", sa.JSON(), nullable=True),
        sa.Column("source_timestamp", sa.String(64), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("test_run_id", "assertion_no", name="uq_automation_assertion_no"),
    )
    op.create_index("ix_assertion_result_run_verdict", "assertion_results", ["test_run_id", "verdict"])
    op.create_index("ix_assertion_result_source", "assertion_results", ["source"])


def downgrade():
    op.drop_index("ix_assertion_result_source", table_name="assertion_results")
    op.drop_index("ix_assertion_result_run_verdict", table_name="assertion_results")
    op.drop_table("assertion_results")
    op.drop_index("ix_automation_test_step_action", table_name="automation_test_step_runs")
    op.drop_index("ix_automation_test_step_run", table_name="automation_test_step_runs")
    op.drop_table("automation_test_step_runs")
    op.drop_index("ix_automation_test_run_checksum", table_name="automation_test_runs")
    op.drop_index("ix_automation_test_run_worker", table_name="automation_test_runs")
    op.drop_index("ix_automation_test_run_status", table_name="automation_test_runs")
    op.drop_index("ix_automation_test_run_case", table_name="automation_test_runs")
    op.drop_table("automation_test_runs")
