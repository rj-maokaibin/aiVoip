"""AI3 Case Copilot V1

Revision ID: 0025_ai_case_copilot_v1
Revises: 0024_ai_semantic_router_v1
Create Date: 2026-08-18
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0025_ai_case_copilot_v1"
down_revision: Union[str, None] = "0024_ai_semantic_router_v1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ai_case_copilot_records",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("case_id", sa.String(36), sa.ForeignKey("cases.id", ondelete="CASCADE"), nullable=False),
        sa.Column("request_key", sa.String(256), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("actor_role", sa.String(32), nullable=False),
        sa.Column("question_hash", sa.String(64), nullable=False),
        sa.Column("snapshot_fingerprint", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("proposal_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("grounding_report_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("routed_control_intent", sa.String(64), nullable=True),
        sa.Column("prompt_version", sa.String(64), nullable=False, server_default="ai-case-copilot-v1"),
        sa.Column("model_name", sa.String(128), nullable=True),
        sa.Column("error_code", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("request_key", name="uq_ai_case_copilot_request_key"),
        sa.CheckConstraint(
            "status IN ('ANSWERED','CONTROL_INTENT_REQUIRED','REJECTED','GATEWAY_FAILED')",
            name="ck_ai_case_copilot_status",
        ),
    )
    op.create_index("ix_ai_case_copilot_case", "ai_case_copilot_records", ["case_id"])
    op.create_index("ix_ai_case_copilot_status", "ai_case_copilot_records", ["status"])
    op.create_index("ix_ai_case_copilot_created", "ai_case_copilot_records", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_ai_case_copilot_created", table_name="ai_case_copilot_records")
    op.drop_index("ix_ai_case_copilot_status", table_name="ai_case_copilot_records")
    op.drop_index("ix_ai_case_copilot_case", table_name="ai_case_copilot_records")
    op.drop_table("ai_case_copilot_records")
