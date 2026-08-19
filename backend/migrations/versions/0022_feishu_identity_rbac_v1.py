"""Feishu identity and Case ACL governance V1

Revision ID: 0022_feishu_identity_rbac_v1
Revises: 0021_feishu_case_governance_v1
Create Date: 2026-08-18
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0022_feishu_identity_rbac_v1"
down_revision: Union[str, None] = "0021_feishu_case_governance_v1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "feishu_user_identities",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_key", sa.String(128), nullable=False),
        sa.Column("open_id", sa.String(128), nullable=False),
        sa.Column("union_id", sa.String(128), nullable=True),
        sa.Column("user_id", sa.String(128), nullable=True),
        sa.Column("internal_actor_id", sa.String(128), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="ACTIVE"),
        sa.Column("display_name", sa.String(256), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_key", "open_id", name="uq_feishu_identity_tenant_open_id"),
    )
    op.create_index("ix_feishu_identity_tenant", "feishu_user_identities", ["tenant_key"])
    op.create_index("ix_feishu_identity_open_id", "feishu_user_identities", ["open_id"])
    op.create_index("ix_feishu_identity_union_id", "feishu_user_identities", ["union_id"])
    op.create_index("ix_feishu_identity_user_id", "feishu_user_identities", ["user_id"])
    op.create_index("ix_feishu_identity_actor", "feishu_user_identities", ["internal_actor_id"])
    op.create_index("ix_feishu_identity_role", "feishu_user_identities", ["role"])
    op.create_index("ix_feishu_identity_status", "feishu_user_identities", ["status"])
    op.create_index("ix_feishu_identity_last_seen", "feishu_user_identities", ["last_seen_at"])

    op.create_table(
        "case_acl_entries",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("case_id", sa.String(36), sa.ForeignKey("cases.id", ondelete="CASCADE"), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("capability", sa.String(64), nullable=False),
        sa.Column("effect", sa.String(8), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("case_id", "actor_id", "capability", name="uq_case_acl_actor_capability"),
        sa.CheckConstraint("effect IN ('ALLOW','DENY')", name="ck_case_acl_effect"),
    )
    op.create_index("ix_case_acl_case", "case_acl_entries", ["case_id"])
    op.create_index("ix_case_acl_actor", "case_acl_entries", ["actor_id"])
    op.create_index("ix_case_acl_capability", "case_acl_entries", ["capability"])
    op.create_index("ix_case_acl_effect", "case_acl_entries", ["effect"])
    op.create_index("ix_case_acl_expires", "case_acl_entries", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_case_acl_expires", table_name="case_acl_entries")
    op.drop_index("ix_case_acl_effect", table_name="case_acl_entries")
    op.drop_index("ix_case_acl_capability", table_name="case_acl_entries")
    op.drop_index("ix_case_acl_actor", table_name="case_acl_entries")
    op.drop_index("ix_case_acl_case", table_name="case_acl_entries")
    op.drop_table("case_acl_entries")

    op.drop_index("ix_feishu_identity_last_seen", table_name="feishu_user_identities")
    op.drop_index("ix_feishu_identity_status", table_name="feishu_user_identities")
    op.drop_index("ix_feishu_identity_role", table_name="feishu_user_identities")
    op.drop_index("ix_feishu_identity_actor", table_name="feishu_user_identities")
    op.drop_index("ix_feishu_identity_user_id", table_name="feishu_user_identities")
    op.drop_index("ix_feishu_identity_union_id", table_name="feishu_user_identities")
    op.drop_index("ix_feishu_identity_open_id", table_name="feishu_user_identities")
    op.drop_index("ix_feishu_identity_tenant", table_name="feishu_user_identities")
    op.drop_table("feishu_user_identities")