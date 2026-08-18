"""Evidence retention V1

Revision ID: 0020_evidence_retention_v1
Revises: 0019_preliminary_evidence_report_v1
Create Date: 2026-08-18
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0020_evidence_retention_v1"
down_revision: Union[str, None] = "0019_preliminary_evidence_report_v1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "evidence_retention_states",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("evidence_id", sa.String(36), nullable=False),
        sa.Column("case_id", sa.String(36), nullable=False),
        sa.Column("policy", sa.String(32), nullable=False),
        sa.Column("retain_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_by", sa.String(128), nullable=True),
        sa.Column("lock_reason", sa.Text(), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("golden_exempt", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("expired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("object_deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["evidence_id"], ["evidences.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("evidence_id", name="uq_evidence_retention_evidence"),
    )
    op.create_index("ix_evidence_retention_states_evidence_id", "evidence_retention_states", ["evidence_id"])
    op.create_index("ix_evidence_retention_states_case_id", "evidence_retention_states", ["case_id"])
    op.create_index("ix_evidence_retention_states_policy", "evidence_retention_states", ["policy"])
    op.create_index("ix_evidence_retention_states_retain_until", "evidence_retention_states", ["retain_until"])
    op.create_index("ix_evidence_retention_states_golden_exempt", "evidence_retention_states", ["golden_exempt"])
    op.create_index("ix_evidence_retention_states_expired_at", "evidence_retention_states", ["expired_at"])
    op.create_index("ix_evidence_retention_states_status", "evidence_retention_states", ["status"])


def downgrade() -> None:
    op.drop_table("evidence_retention_states")
