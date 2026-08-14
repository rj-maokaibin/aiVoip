"""feishu_case_bindings.message_id nullable

Revision ID: 0013_feishu_binding_nullable_message
Revises: 0012_device_credentials
Create Date: 2026-08-14

A Case can be bound to its source Feishu chat (chat_id) at provision time,
before any card is ever sent. feishu_case_bindings.message_id must therefore be
nullable (backfilled on first sync_case_card); the binding row carries the
receive_id (source chat) from the start.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0013_feishu_binding_nullable_message'
down_revision: Union[str, None] = '0012_device_credentials'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column('feishu_case_bindings', 'message_id',
                    existing_type=sa.String(256), nullable=True)


def downgrade() -> None:
    op.alter_column('feishu_case_bindings', 'message_id',
                    existing_type=sa.String(256), nullable=False)
