"""AI2 Diagnostic Loop V1

Revision ID: 0026_ai_diagnostic_loop_v1
Revises: 0025_ai_case_copilot_v1
Create Date: 2026-08-19
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0026_ai_diagnostic_loop_v1"
down_revision: Union[str, None] = "0025_ai_case_copilot_v1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ai_diagnostic_cycles",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("case_id", sa.String(36), sa.ForeignKey("cases.id", ondelete="CASCADE"), nullable=False),
        sa.Column("cycle_no", sa.Integer(), nullable=False),
        sa.Column("runtime_stage", sa.String(24), nullable=False),
        sa.Column("snapshot_fingerprint", sa.String(64), nullable=False),
        sa.Column("evidence_fingerprint", sa.String(64), nullable=False),
        sa.Column("proposal_id", sa.String(36), sa.ForeignKey("ai_proposals.id", ondelete="SET NULL"), nullable=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("known_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
        sa.Column("unknown_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
        sa.Column("excluded_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
        sa.Column("hypotheses_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
        sa.Column("critic_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("next_action_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("selection_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("continue_recommendation", sa.String(24), nullable=False),
        sa.Column("stop_reason", sa.String(128), nullable=True),
        sa.Column("no_progress_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("formal_result_changed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("dispatch_attempted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("dispatch_allowed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("suggestion_state", sa.String(24), nullable=False, server_default="NONE"),
        sa.Column("accepted_by", sa.String(128), nullable=True),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("execution_ref_type", sa.String(64), nullable=True),
        sa.Column("execution_ref_id", sa.String(64), nullable=True),
        sa.Column("suggestion_error_code", sa.String(128), nullable=True),
        sa.Column("error_code", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("case_id", "cycle_no", name="uq_ai_diagnostic_cycle_case_no"),
        sa.UniqueConstraint(
            "case_id", "snapshot_fingerprint", "runtime_stage",
            name="uq_ai_diagnostic_cycle_snapshot_stage",
        ),
        sa.CheckConstraint("runtime_stage IN ('SHADOW','SUGGEST')", name="ck_ai_diagnostic_cycle_stage"),
        sa.CheckConstraint(
            "status IN ('COMPLETED','DEGRADED','STOPPED','REQUIRE_HUMAN')",
            name="ck_ai_diagnostic_cycle_status",
        ),
        sa.CheckConstraint(
            "continue_recommendation IN ('CONTINUE','STOP','REQUIRE_HUMAN')",
            name="ck_ai_diagnostic_cycle_continue",
        ),
        sa.CheckConstraint(
            "suggestion_state IN ('NONE','PROPOSED','ACCEPTED','DISPATCHED','FAILED')",
            name="ck_ai_diagnostic_cycle_suggestion_state",
        ),
        sa.CheckConstraint(
            "formal_result_changed = false",
            name="ck_ai_diagnostic_cycle_no_formal_result_change",
        ),
        sa.CheckConstraint(
            "dispatch_attempted = false",
            name="ck_ai_diagnostic_cycle_no_ai_dispatch_attempt",
        ),
        sa.CheckConstraint(
            "dispatch_allowed = false",
            name="ck_ai_diagnostic_cycle_no_ai_dispatch_authority",
        ),
    )
    op.create_index("ix_ai_diagnostic_cycle_case", "ai_diagnostic_cycles", ["case_id"])
    op.create_index("ix_ai_diagnostic_cycle_stage", "ai_diagnostic_cycles", ["runtime_stage"])
    op.create_index("ix_ai_diagnostic_cycle_status", "ai_diagnostic_cycles", ["status"])
    op.create_index("ix_ai_diagnostic_cycle_suggestion_state", "ai_diagnostic_cycles", ["suggestion_state"])
    op.create_index("ix_ai_diagnostic_cycle_created", "ai_diagnostic_cycles", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_ai_diagnostic_cycle_created", table_name="ai_diagnostic_cycles")
    op.drop_index("ix_ai_diagnostic_cycle_suggestion_state", table_name="ai_diagnostic_cycles")
    op.drop_index("ix_ai_diagnostic_cycle_status", table_name="ai_diagnostic_cycles")
    op.drop_index("ix_ai_diagnostic_cycle_stage", table_name="ai_diagnostic_cycles")
    op.drop_index("ix_ai_diagnostic_cycle_case", table_name="ai_diagnostic_cycles")
    op.drop_table("ai_diagnostic_cycles")
