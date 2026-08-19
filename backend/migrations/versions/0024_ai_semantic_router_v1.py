"""AI1 semantic router V1

Revision ID: 0024_ai_semantic_router_v1
Revises: 0023_feishu_document_acl_v1
Create Date: 2026-08-18
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0024_ai_semantic_router_v1"
down_revision: Union[str, None] = "0023_feishu_document_acl_v1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ai_semantic_intent_records",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("case_id", sa.String(36), sa.ForeignKey("cases.id", ondelete="SET NULL"), nullable=True),
        sa.Column("tenant_key", sa.String(128), nullable=True),
        sa.Column("chat_id", sa.String(256), nullable=True),
        sa.Column("message_id", sa.String(256), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("deterministic_intent", sa.String(48), nullable=False),
        sa.Column("deterministic_confidence", sa.Float(), nullable=False),
        sa.Column("proposal_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("validated_intent", sa.String(48), nullable=True),
        sa.Column("ai_confidence", sa.Float(), nullable=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("prompt_version", sa.String(64), nullable=False, server_default="feishu-semantic-router-v1"),
        sa.Column("model_name", sa.String(128), nullable=True),
        sa.Column("error_code", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("message_id", name="uq_ai_semantic_intent_message_id"),
        sa.CheckConstraint(
            "status IN ('SHADOW_VALID','REJECTED','BYPASSED','GATEWAY_FAILED')",
            name="ck_ai_semantic_intent_status",
        ),
    )
    op.create_index("ix_ai_semantic_intent_case", "ai_semantic_intent_records", ["case_id"])
    op.create_index("ix_ai_semantic_intent_chat", "ai_semantic_intent_records", ["tenant_key", "chat_id"])
    op.create_index("ix_ai_semantic_intent_status", "ai_semantic_intent_records", ["status"])
    op.create_index("ix_ai_semantic_intent_created", "ai_semantic_intent_records", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_ai_semantic_intent_created", table_name="ai_semantic_intent_records")
    op.drop_index("ix_ai_semantic_intent_status", table_name="ai_semantic_intent_records")
    op.drop_index("ix_ai_semantic_intent_chat", table_name="ai_semantic_intent_records")
    op.drop_index("ix_ai_semantic_intent_case", table_name="ai_semantic_intent_records")
    op.drop_table("ai_semantic_intent_records")
