"""persist Feishu source message/thread context

Revision ID: 0014_feishu_source_context
Revises: 0013_feishu_binding_nullable_message
Create Date: 2026-08-15
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0014_feishu_source_context'
down_revision: Union[str, None] = '0013_feishu_binding_nullable_message'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('feishu_case_bindings', sa.Column('source_event_id', sa.String(256), nullable=True))
    op.add_column('feishu_case_bindings', sa.Column('source_message_id', sa.String(256), nullable=True))
    op.add_column('feishu_case_bindings', sa.Column('source_root_message_id', sa.String(256), nullable=True))
    op.add_column('feishu_case_bindings', sa.Column('source_parent_message_id', sa.String(256), nullable=True))
    op.add_column('feishu_case_bindings', sa.Column('source_sender_open_id', sa.String(256), nullable=True))
    op.add_column('feishu_case_bindings', sa.Column('source_chat_type', sa.String(32), nullable=True))
    op.create_index('ix_feishu_case_bindings_source_event_id', 'feishu_case_bindings', ['source_event_id'])
    op.create_index('ix_feishu_case_bindings_source_message_id', 'feishu_case_bindings', ['source_message_id'])
    op.create_index('ix_feishu_case_bindings_source_root_message_id', 'feishu_case_bindings', ['source_root_message_id'])


def downgrade() -> None:
    op.drop_index('ix_feishu_case_bindings_source_root_message_id', table_name='feishu_case_bindings')
    op.drop_index('ix_feishu_case_bindings_source_message_id', table_name='feishu_case_bindings')
    op.drop_index('ix_feishu_case_bindings_source_event_id', table_name='feishu_case_bindings')
    op.drop_column('feishu_case_bindings', 'source_chat_type')
    op.drop_column('feishu_case_bindings', 'source_sender_open_id')
    op.drop_column('feishu_case_bindings', 'source_parent_message_id')
    op.drop_column('feishu_case_bindings', 'source_root_message_id')
    op.drop_column('feishu_case_bindings', 'source_message_id')
    op.drop_column('feishu_case_bindings', 'source_event_id')
