"""complete Feishu intake source context

Revision ID: 0016_feishu_intake_context
Revises: 0015_ai_proposal_shadow
Create Date: 2026-08-16
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0016_feishu_intake_context'
down_revision: Union[str, None] = '0015_ai_proposal_shadow'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('feishu_case_bindings', sa.Column('source_tenant_key', sa.String(256), nullable=True))
    op.add_column('feishu_case_bindings', sa.Column('source_message_timestamp', sa.String(32), nullable=True))
    op.add_column('feishu_case_bindings', sa.Column('source_normalized_text', sa.Text(), nullable=True))
    op.add_column('feishu_case_bindings', sa.Column('source_attachment_refs', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('feishu_case_bindings', 'source_attachment_refs')
    op.drop_column('feishu_case_bindings', 'source_normalized_text')
    op.drop_column('feishu_case_bindings', 'source_message_timestamp')
    op.drop_column('feishu_case_bindings', 'source_tenant_key')
