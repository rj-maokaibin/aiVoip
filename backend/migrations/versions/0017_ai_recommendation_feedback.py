"""AI recommendation feedback for Eval acceptance metrics

Revision ID: 0017_ai_recommendation_feedback
Revises: 0016_feishu_intake_context
Create Date: 2026-08-16
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0017_ai_recommendation_feedback'
down_revision: Union[str, None] = '0016_feishu_intake_context'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'ai_recommendation_feedback',
        sa.Column('id',sa.String(36),nullable=False),
        sa.Column('proposal_id',sa.String(36),nullable=False),
        sa.Column('case_id',sa.String(36),nullable=False),
        sa.Column('item_type',sa.String(32),nullable=False),
        sa.Column('decision',sa.String(32),nullable=False),
        sa.Column('actor',sa.String(128),nullable=False),
        sa.Column('reason',sa.Text(),nullable=True),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),
        sa.ForeignKeyConstraint(['proposal_id'],['ai_proposals.id'],ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['case_id'],['cases.id'],ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    for column in ('proposal_id','case_id','item_type','decision','actor','created_at'):
        op.create_index(f'ix_ai_recommendation_feedback_{column}','ai_recommendation_feedback',[column])


def downgrade() -> None:
    op.drop_table('ai_recommendation_feedback')
