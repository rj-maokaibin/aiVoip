"""AI proposal Shadow Mode audit records

Revision ID: 0015_ai_proposal_shadow
Revises: 0014_feishu_source_context
Create Date: 2026-08-15
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0015_ai_proposal_shadow'
down_revision: Union[str, None] = '0014_feishu_source_context'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'ai_proposals',
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('case_id', sa.String(36), nullable=False),
        sa.Column('diagnosis_run_id', sa.String(36), nullable=True),
        sa.Column('schema_version', sa.String(64), nullable=False),
        sa.Column('intent', sa.String(64), nullable=False),
        sa.Column('mode', sa.String(32), nullable=False),
        sa.Column('status', sa.String(32), nullable=False),
        sa.Column('input_fingerprint', sa.String(64), nullable=False),
        sa.Column('model_name', sa.String(128), nullable=True),
        sa.Column('prompt_version', sa.String(64), nullable=True),
        sa.Column('workflow_version', sa.String(64), nullable=False),
        sa.Column('latency_ms', sa.Integer(), nullable=True),
        sa.Column('raw_output_json', sa.JSON(), nullable=True),
        sa.Column('validated_output_json', sa.JSON(), nullable=True),
        sa.Column('validation_errors', sa.JSON(), nullable=True),
        sa.Column('baseline_json', sa.JSON(), nullable=False),
        sa.Column('diff_json', sa.JSON(), nullable=True),
        sa.Column('gateway_error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['case_id'], ['cases.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['diagnosis_run_id'], ['diagnosis_runs.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_ai_proposals_case_id', 'ai_proposals', ['case_id'])
    op.create_index('ix_ai_proposals_diagnosis_run_id', 'ai_proposals', ['diagnosis_run_id'])
    op.create_index('ix_ai_proposals_mode', 'ai_proposals', ['mode'])
    op.create_index('ix_ai_proposals_status', 'ai_proposals', ['status'])
    op.create_index('ix_ai_proposals_input_fingerprint', 'ai_proposals', ['input_fingerprint'])
    op.create_index('ix_ai_proposals_created_at', 'ai_proposals', ['created_at'])


def downgrade() -> None:
    op.drop_table('ai_proposals')
