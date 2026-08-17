"""Persist automatic Golden Candidate eligibility state

Revision ID: 0018_golden_candidate_assessments
Revises: 0017_ai_recommendation_feedback
Create Date: 2026-08-17
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0018_golden_candidate_assessments'
down_revision: Union[str, None] = '0017_ai_recommendation_feedback'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'golden_candidate_assessments',
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('case_id', sa.String(36), nullable=False),
        sa.Column('status', sa.String(32), nullable=False),
        sa.Column('verification_tier', sa.String(8), nullable=True),
        sa.Column('assessment_version', sa.String(64), nullable=False),
        sa.Column('score', sa.Integer(), nullable=False),
        sa.Column('root_cause_confirmed', sa.Integer(), nullable=False),
        sa.Column('fix_verified', sa.Integer(), nullable=False),
        sa.Column('direct_l1_support', sa.Integer(), nullable=False),
        sa.Column('deterministic_baseline_ready', sa.Integer(), nullable=False),
        sa.Column('snapshot_ready', sa.Integer(), nullable=False),
        sa.Column('audit_coverage_complete', sa.Integer(), nullable=False),
        sa.Column('answer_leakage_risk', sa.Integer(), nullable=False),
        sa.Column('evidence_count', sa.Integer(), nullable=False),
        sa.Column('complete_evidence_count', sa.Integer(), nullable=False),
        sa.Column('l1_evidence_count', sa.Integer(), nullable=False),
        sa.Column('successful_analyzer_count', sa.Integer(), nullable=False),
        sa.Column('confirmed_hypothesis_count', sa.Integer(), nullable=False),
        sa.Column('blocker_codes', sa.JSON(), nullable=False),
        sa.Column('gap_codes', sa.JSON(), nullable=False),
        sa.Column('next_steps', sa.JSON(), nullable=False),
        sa.Column('leakage_findings', sa.JSON(), nullable=False),
        sa.Column('details_json', sa.JSON(), nullable=False),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('assessed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['case_id'], ['cases.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('case_id', name='uq_golden_candidate_assessments_case_id'),
    )
    op.create_index('ix_golden_candidate_assessments_case_id', 'golden_candidate_assessments', ['case_id'])
    op.create_index('ix_golden_candidate_assessments_status', 'golden_candidate_assessments', ['status'])
    op.create_index('ix_golden_candidate_assessments_verification_tier', 'golden_candidate_assessments', ['verification_tier'])


def downgrade() -> None:
    op.drop_table('golden_candidate_assessments')
