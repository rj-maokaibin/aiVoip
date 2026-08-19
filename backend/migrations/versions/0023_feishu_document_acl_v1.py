"""Feishu document ACL synchronization V1

Revision ID: 0023_feishu_document_acl_v1
Revises: 0022_feishu_identity_rbac_v1
Create Date: 2026-08-18
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0023_feishu_document_acl_v1"
down_revision: Union[str, None] = "0022_feishu_identity_rbac_v1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "feishu_document_acl_bindings",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("case_id", sa.String(36), sa.ForeignKey("cases.id", ondelete="CASCADE"), nullable=False),
        sa.Column("document_id", sa.String(256), nullable=False),
        sa.Column("tenant_key", sa.String(128), nullable=False),
        sa.Column("chat_id", sa.String(256), nullable=False),
        sa.Column("sync_mode", sa.String(24), nullable=False, server_default="AUTO"),
        sa.Column("effective_mode", sa.String(24), nullable=True),
        sa.Column("desired_permission", sa.String(16), nullable=False, server_default="view"),
        sa.Column("desired_revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("applied_revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(16), nullable=False, server_default="PENDING"),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(1000), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("case_id", "document_id", name="uq_feishu_document_acl_case_document"),
        sa.CheckConstraint("sync_mode IN ('AUTO','CHAT_SCOPE','MEMBER_MIRROR')", name="ck_feishu_document_acl_mode"),
        sa.CheckConstraint("desired_permission IN ('view','edit','full_access')", name="ck_feishu_document_acl_permission"),
        sa.CheckConstraint("status IN ('PENDING','SYNCING','SYNCED','PARTIAL','FAILED')", name="ck_feishu_document_acl_status"),
    )
    op.create_index("ix_feishu_document_acl_case", "feishu_document_acl_bindings", ["case_id"])
    op.create_index("ix_feishu_document_acl_document", "feishu_document_acl_bindings", ["document_id"])
    op.create_index("ix_feishu_document_acl_status", "feishu_document_acl_bindings", ["status"])
    op.create_index("ix_feishu_document_acl_chat", "feishu_document_acl_bindings", ["tenant_key", "chat_id"])


def downgrade() -> None:
    op.drop_index("ix_feishu_document_acl_chat", table_name="feishu_document_acl_bindings")
    op.drop_index("ix_feishu_document_acl_status", table_name="feishu_document_acl_bindings")
    op.drop_index("ix_feishu_document_acl_document", table_name="feishu_document_acl_bindings")
    op.drop_index("ix_feishu_document_acl_case", table_name="feishu_document_acl_bindings")
    op.drop_table("feishu_document_acl_bindings")
