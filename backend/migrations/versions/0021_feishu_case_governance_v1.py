"""Feishu one-group-one-active-case governance V1

Revision ID: 0021_feishu_case_governance_v1
Revises: 0020_evidence_retention_v1
Create Date: 2026-08-18
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0021_feishu_case_governance_v1"
down_revision: Union[str, None] = "0020_evidence_retention_v1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TERMINAL_CASE_STATES = ("RESOLVED", "CLOSED", "FAILED")


def upgrade() -> None:
    op.add_column("feishu_case_bindings", sa.Column("binding_state", sa.String(16), nullable=False, server_default="ACTIVE"))
    op.add_column("feishu_case_bindings", sa.Column("binding_generation", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("feishu_case_bindings", sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("feishu_case_bindings", sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("feishu_case_bindings", sa.Column("created_by_open_id", sa.String(128), nullable=True))
    op.add_column("feishu_case_bindings", sa.Column("close_reason", sa.String(128), nullable=True))

    # Legacy/default-delivery bindings may not have tenant identity. Keep them as
    # empty-tenant history, but only real tenant-bound source chats participate in
    # the G1 partial unique invariant. Live Feishu message events always carry a
    # tenant_key; this avoids breaking old single-default-group card delivery.
    op.execute("UPDATE feishu_case_bindings SET source_tenant_key = '' WHERE source_tenant_key IS NULL")
    op.alter_column(
        "feishu_case_bindings", "source_tenant_key", existing_type=sa.String(256),
        nullable=False, server_default="",
    )

    op.execute(
        """
        WITH ranked AS (
            SELECT id,
                   ROW_NUMBER() OVER (
                       PARTITION BY source_tenant_key, receive_id
                       ORDER BY created_at ASC, id ASC
                   ) AS generation
            FROM feishu_case_bindings
            WHERE receive_id_type = 'chat_id'
        )
        UPDATE feishu_case_bindings AS b
        SET binding_generation = ranked.generation,
            activated_at = COALESCE(b.activated_at, b.created_at)
        FROM ranked
        WHERE b.id = ranked.id
        """
    )

    terminal_sql = ",".join(f"'{state}'" for state in _TERMINAL_CASE_STATES)
    op.execute(
        f"""
        UPDATE feishu_case_bindings AS b
        SET binding_state = 'CLOSED',
            closed_at = COALESCE(b.closed_at, CURRENT_TIMESTAMP),
            close_reason = COALESCE(b.close_reason, 'MIGRATION_CASE_TERMINAL')
        FROM cases AS c
        WHERE c.id = b.case_id
          AND c.status IN ({terminal_sql})
        """
    )

    op.execute(
        """
        WITH active_ranked AS (
            SELECT id,
                   ROW_NUMBER() OVER (
                       PARTITION BY source_tenant_key, receive_id
                       ORDER BY created_at DESC, id DESC
                   ) AS rn
            FROM feishu_case_bindings
            WHERE receive_id_type = 'chat_id'
              AND binding_state = 'ACTIVE'
              AND source_tenant_key <> ''
        )
        UPDATE feishu_case_bindings AS b
        SET binding_state = 'CLOSED',
            closed_at = COALESCE(b.closed_at, CURRENT_TIMESTAMP),
            close_reason = COALESCE(b.close_reason, 'MIGRATION_SUPERSEDED')
        FROM active_ranked AS ranked
        WHERE b.id = ranked.id
          AND ranked.rn > 1
        """
    )

    op.create_index(
        "uq_feishu_active_case_per_chat", "feishu_case_bindings",
        ["source_tenant_key", "receive_id"], unique=True,
        postgresql_where=sa.text(
            "binding_state = 'ACTIVE' AND receive_id_type = 'chat_id' AND source_tenant_key <> ''"
        ),
    )
    op.create_index("ix_feishu_case_bindings_binding_state", "feishu_case_bindings", ["binding_state"])


def downgrade() -> None:
    op.drop_index("ix_feishu_case_bindings_binding_state", table_name="feishu_case_bindings")
    op.drop_index("uq_feishu_active_case_per_chat", table_name="feishu_case_bindings")
    op.alter_column(
        "feishu_case_bindings", "source_tenant_key", existing_type=sa.String(256),
        nullable=True, server_default=None,
    )
    op.drop_column("feishu_case_bindings", "close_reason")
    op.drop_column("feishu_case_bindings", "created_by_open_id")
    op.drop_column("feishu_case_bindings", "closed_at")
    op.drop_column("feishu_case_bindings", "activated_at")
    op.drop_column("feishu_case_bindings", "binding_generation")
    op.drop_column("feishu_case_bindings", "binding_state")
