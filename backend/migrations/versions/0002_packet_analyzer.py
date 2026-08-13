"""packet analyzer run model
Revision ID: 0002
Revises: 0001
"""
from alembic import op
import sqlalchemy as sa
revision='0002'; down_revision='0001'; branch_labels=None; depends_on=None


def upgrade():
    op.create_table('analyzer_runs',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('case_id', sa.String(36), sa.ForeignKey('cases.id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('job_id', sa.String(36), sa.ForeignKey('jobs.id', ondelete='SET NULL'), nullable=True, index=True),
        sa.Column('analyzer_name', sa.String(128), nullable=False, index=True),
        sa.Column('analyzer_version', sa.String(64), nullable=False),
        sa.Column('config_version', sa.String(128), nullable=True),
        sa.Column('status', sa.String(32), nullable=False, index=True),
        sa.Column('input_evidence_ids', sa.JSON(), nullable=False),
        sa.Column('summary_json', sa.JSON(), nullable=True),
        sa.Column('result_object_key', sa.String(1024), nullable=True),
        sa.Column('error_code', sa.String(128), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False))


def downgrade():
    op.drop_table('analyzer_runs')
