"""Conversation Platform P0/P1 + Knowledge ProductFact V1

Revision ID: 0027_conversation_knowledge_v1
Revises: 0026_ai_diagnostic_loop_v1
Create Date: 2026-08-29
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0027_conversation_knowledge_v1"
down_revision: Union[str, None] = "0026_ai_diagnostic_loop_v1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_key", sa.String(256), nullable=False, server_default=""),
        sa.Column("channel", sa.String(32), nullable=False, server_default="FEISHU"),
        sa.Column("chat_id", sa.String(256), nullable=False),
        sa.Column("active_case_id", sa.String(36), sa.ForeignKey("cases.id", ondelete="SET NULL"), nullable=True),
        sa.Column("active_topic", sa.String(256), nullable=True),
        sa.Column("entities_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("turn_no", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(24), nullable=False, server_default="ACTIVE"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_key", "channel", "chat_id", name="uq_conversation_channel_chat"),
    )
    op.create_index("ix_conversations_tenant_key", "conversations", ["tenant_key"])
    op.create_index("ix_conversations_channel", "conversations", ["channel"])
    op.create_index("ix_conversations_chat_id", "conversations", ["chat_id"])
    op.create_index("ix_conversations_active_case_id", "conversations", ["active_case_id"])
    op.create_index("ix_conversations_status", "conversations", ["status"])

    op.create_table(
        "conversation_states",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("conversation_id", sa.String(36), sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("active_question_json", sa.JSON(), nullable=True),
        sa.Column("slots_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("unavailable_needs_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
        sa.Column("last_user_intent", sa.String(64), nullable=True),
        sa.Column("last_progress_digest", sa.String(64), nullable=True),
        sa.Column("material_context_hash", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("conversation_id", name="uq_conversation_state_conversation"),
    )
    op.create_index("ix_conversation_states_conversation_id", "conversation_states", ["conversation_id"], unique=True)
    op.create_index("ix_conversation_states_last_user_intent", "conversation_states", ["last_user_intent"])
    op.create_index("ix_conversation_states_material_context_hash", "conversation_states", ["material_context_hash"])

    op.create_table(
        "conversation_turns",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("conversation_id", sa.String(36), sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("case_id", sa.String(36), sa.ForeignKey("cases.id", ondelete="SET NULL"), nullable=True),
        sa.Column("source_message_id", sa.String(256), nullable=True),
        sa.Column("sender_id", sa.String(256), nullable=True),
        sa.Column("direction", sa.String(16), nullable=False, server_default="USER"),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("intent", sa.String(64), nullable=False),
        sa.Column("classification", sa.String(32), nullable=False),
        sa.Column("route_mode", sa.String(32), nullable=False),
        sa.Column("material_diagnostic_context", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("parsed_json", sa.JSON(), nullable=True),
        sa.Column("evidence_id", sa.String(36), sa.ForeignKey("evidences.id", ondelete="SET NULL"), nullable=True),
        sa.Column("model_name", sa.String(128), nullable=True),
        sa.Column("prompt_version", sa.String(64), nullable=True),
        sa.Column("snapshot_hash", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("conversation_id", "source_message_id", name="uq_conversation_turn_source"),
    )
    for name, columns in (
        ("ix_conversation_turns_conversation_id", ["conversation_id"]),
        ("ix_conversation_turns_case_id", ["case_id"]),
        ("ix_conversation_turns_source_message_id", ["source_message_id"]),
        ("ix_conversation_turns_intent", ["intent"]),
        ("ix_conversation_turns_classification", ["classification"]),
        ("ix_conversation_turns_route_mode", ["route_mode"]),
        ("ix_conversation_turns_material", ["material_diagnostic_context"]),
        ("ix_conversation_turns_evidence_id", ["evidence_id"]),
        ("ix_conversation_turns_snapshot_hash", ["snapshot_hash"]),
        ("ix_conversation_turns_created_at", ["created_at"]),
    ):
        op.create_index(name, "conversation_turns", columns)

    op.create_table(
        "feishu_reply_delivery_traces",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("conversation_id", sa.String(36), sa.ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True),
        sa.Column("case_id", sa.String(36), sa.ForeignKey("cases.id", ondelete="SET NULL"), nullable=True),
        sa.Column("source_message_id", sa.String(256), nullable=False),
        sa.Column("semantic_key", sa.String(64), nullable=False),
        sa.Column("stage", sa.String(32), nullable=False, server_default="ENQUEUED"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sent_message_id", sa.String(256), nullable=True),
        sa.Column("error_code", sa.String(128), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    for name, columns in (
        ("ix_feishu_reply_delivery_source", ["source_message_id"]),
        ("ix_feishu_reply_delivery_semantic", ["semantic_key"]),
        ("ix_feishu_reply_delivery_stage", ["stage"]),
        ("ix_feishu_reply_delivery_error", ["error_code"]),
        ("ix_feishu_reply_delivery_created", ["created_at"]),
        ("ix_feishu_reply_delivery_conversation", ["conversation_id"]),
        ("ix_feishu_reply_delivery_case", ["case_id"]),
    ):
        op.create_index(name, "feishu_reply_delivery_traces", columns)

    op.create_table(
        "product_facts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("product_model", sa.String(128), nullable=False),
        sa.Column("feature_key", sa.String(256), nullable=False),
        sa.Column("value_json", sa.JSON(), nullable=False),
        sa.Column("value_text", sa.Text(), nullable=True),
        sa.Column("unit", sa.String(64), nullable=True),
        sa.Column("hw_scope", sa.String(128), nullable=False, server_default="*"),
        sa.Column("sw_version_scope", sa.String(128), nullable=False, server_default="*"),
        sa.Column("region_scope", sa.String(128), nullable=False, server_default="*"),
        sa.Column("source_document", sa.String(512), nullable=False),
        sa.Column("source_section", sa.String(512), nullable=True),
        sa.Column("source_ref", sa.String(1024), nullable=True),
        sa.Column("authority_level", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("approval_status", sa.String(32), nullable=False, server_default="DRAFT"),
        sa.Column("supersedes_fact_id", sa.String(36), nullable=True),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("effective_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("created_by", sa.String(128), nullable=True),
        sa.Column("approved_by", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "product_model", "feature_key", "hw_scope", "sw_version_scope", "region_scope",
            "effective_from", name="uq_product_fact_scope_version"
        ),
    )
    for name, columns in (
        ("ix_product_facts_model", ["product_model"]),
        ("ix_product_facts_feature", ["feature_key"]),
        ("ix_product_facts_hw_scope", ["hw_scope"]),
        ("ix_product_facts_sw_scope", ["sw_version_scope"]),
        ("ix_product_facts_region_scope", ["region_scope"]),
        ("ix_product_facts_authority", ["authority_level"]),
        ("ix_product_facts_approval", ["approval_status"]),
        ("ix_product_facts_supersedes", ["supersedes_fact_id"]),
        ("ix_product_facts_effective_from", ["effective_from"]),
        ("ix_product_facts_effective_to", ["effective_to"]),
        ("ix_product_facts_created", ["created_at"]),
    ):
        op.create_index(name, "product_facts", columns)


def downgrade() -> None:
    op.drop_table("product_facts")
    op.drop_table("feishu_reply_delivery_traces")
    op.drop_table("conversation_turns")
    op.drop_table("conversation_states")
    op.drop_table("conversations")
