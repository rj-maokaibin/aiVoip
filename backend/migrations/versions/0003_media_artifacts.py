"""media artifacts

Revision ID: 0003
Revises: 0002
"""
from alembic import op
import sqlalchemy as sa

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'artifacts',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('case_id', sa.String(length=36), sa.ForeignKey('cases.id', ondelete='CASCADE'), nullable=False),
        sa.Column('analyzer_run_id', sa.String(length=36), sa.ForeignKey('analyzer_runs.id', ondelete='SET NULL'), nullable=True),
        sa.Column('evidence_id', sa.String(length=36), sa.ForeignKey('evidences.id', ondelete='SET NULL'), nullable=True),
        sa.Column('type', sa.String(length=64), nullable=False),
        sa.Column('filename', sa.String(length=512), nullable=False),
        sa.Column('object_key', sa.String(length=1024), nullable=False, unique=True),
        sa.Column('content_type', sa.String(length=128), nullable=True),
        sa.Column('size_bytes', sa.BigInteger(), nullable=False),
        sa.Column('sha256', sa.String(length=64), nullable=False),
        sa.Column('metadata_json', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_artifacts_case_id', 'artifacts', ['case_id'])
    op.create_index('ix_artifacts_analyzer_run_id', 'artifacts', ['analyzer_run_id'])
    op.create_index('ix_artifacts_evidence_id', 'artifacts', ['evidence_id'])
    op.create_index('ix_artifacts_type', 'artifacts', ['type'])
    op.create_index('ix_artifacts_sha256', 'artifacts', ['sha256'])


def downgrade():
    op.drop_table('artifacts')
