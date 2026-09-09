"""PBX dial alias contract for non-numeric extension identities.

Revision ID: 0035_pbx_dial_alias_v1
Revises: 0034_pbx_test_lab_v1
Create Date: 2026-09-09
"""
from alembic import op
import sqlalchemy as sa

revision = "0035_pbx_dial_alias_v1"
down_revision = "0034_pbx_test_lab_v1"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "pbx_extension_resources",
        sa.Column("dial_alias", sa.String(32), nullable=True),
    )
    op.create_index(
        "ix_pbx_extension_dial_alias",
        "pbx_extension_resources",
        ["pbx_node_id", "domain_id", "dial_alias"],
        unique=True,
    )


def downgrade():
    op.drop_index("ix_pbx_extension_dial_alias", table_name="pbx_extension_resources")
    op.drop_column("pbx_extension_resources", "dial_alias")
