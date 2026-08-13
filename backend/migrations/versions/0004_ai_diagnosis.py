"""m4 ai diagnosis orchestrator
Revision ID: 0004
Revises: 0003
"""
from alembic import op
import sqlalchemy as sa
revision='0004'; down_revision='0003'; branch_labels=None; depends_on=None

def upgrade():
    op.create_table('diagnosis_runs',
        sa.Column('id',sa.String(36),primary_key=True),
        sa.Column('case_id',sa.String(36),sa.ForeignKey('cases.id',ondelete='CASCADE'),nullable=False),
        sa.Column('job_id',sa.String(36),sa.ForeignKey('jobs.id',ondelete='SET NULL'),nullable=True),
        sa.Column('status',sa.String(32),nullable=False),sa.Column('cycle',sa.Integer(),nullable=False),
        sa.Column('reasoner_name',sa.String(128),nullable=False),sa.Column('reasoner_version',sa.String(64),nullable=False),
        sa.Column('workflow_version',sa.String(64),nullable=False),sa.Column('prompt_version',sa.String(64),nullable=True),sa.Column('model_name',sa.String(128),nullable=True),
        sa.Column('last_fingerprint',sa.String(64),nullable=True),sa.Column('no_progress_count',sa.Integer(),nullable=False),
        sa.Column('summary_json',sa.JSON(),nullable=True),sa.Column('decision_json',sa.JSON(),nullable=True),
        sa.Column('started_at',sa.DateTime(timezone=True),nullable=True),sa.Column('finished_at',sa.DateTime(timezone=True),nullable=True),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),sa.Column('updated_at',sa.DateTime(timezone=True),nullable=False))
    op.create_index('ix_diagnosis_runs_case_id','diagnosis_runs',['case_id']); op.create_index('ix_diagnosis_runs_job_id','diagnosis_runs',['job_id']); op.create_index('ix_diagnosis_runs_status','diagnosis_runs',['status'])
    op.create_table('hypotheses',
        sa.Column('id',sa.String(36),primary_key=True),sa.Column('case_id',sa.String(36),sa.ForeignKey('cases.id',ondelete='CASCADE'),nullable=False),
        sa.Column('diagnosis_run_id',sa.String(36),sa.ForeignKey('diagnosis_runs.id',ondelete='SET NULL'),nullable=True),sa.Column('code',sa.String(128),nullable=False),
        sa.Column('title',sa.Text(),nullable=False),sa.Column('fault_domain',sa.String(128),nullable=False),sa.Column('status',sa.String(32),nullable=False),
        sa.Column('confidence',sa.Integer(),nullable=False),sa.Column('rationale',sa.Text(),nullable=True),sa.Column('confirmable',sa.Integer(),nullable=False),sa.Column('confirm_rule',sa.String(128),nullable=True),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),sa.Column('updated_at',sa.DateTime(timezone=True),nullable=False),
        sa.UniqueConstraint('case_id','code',name='uq_hypotheses_case_code'))
    op.create_index('ix_hypotheses_case_id','hypotheses',['case_id']); op.create_index('ix_hypotheses_diagnosis_run_id','hypotheses',['diagnosis_run_id']); op.create_index('ix_hypotheses_code','hypotheses',['code']); op.create_index('ix_hypotheses_fault_domain','hypotheses',['fault_domain']); op.create_index('ix_hypotheses_status','hypotheses',['status'])
    op.create_table('hypothesis_evidence',
        sa.Column('id',sa.String(36),primary_key=True),sa.Column('hypothesis_id',sa.String(36),sa.ForeignKey('hypotheses.id',ondelete='CASCADE'),nullable=False),
        sa.Column('ref_type',sa.String(64),nullable=False),sa.Column('ref_id',sa.String(128),nullable=False),sa.Column('evidence_level',sa.String(8),nullable=False),
        sa.Column('direction',sa.String(16),nullable=False),sa.Column('weight',sa.Integer(),nullable=False),sa.Column('rationale',sa.Text(),nullable=True),sa.Column('details_json',sa.JSON(),nullable=True),sa.Column('created_at',sa.DateTime(timezone=True),nullable=False))
    op.create_index('ix_hypothesis_evidence_hypothesis_id','hypothesis_evidence',['hypothesis_id']); op.create_index('ix_hypothesis_evidence_ref_id','hypothesis_evidence',['ref_id']); op.create_index('ix_hypothesis_evidence_evidence_level','hypothesis_evidence',['evidence_level'])
    op.create_table('collection_plans',
        sa.Column('id',sa.String(36),primary_key=True),sa.Column('case_id',sa.String(36),sa.ForeignKey('cases.id',ondelete='CASCADE'),nullable=False),
        sa.Column('diagnosis_run_id',sa.String(36),sa.ForeignKey('diagnosis_runs.id',ondelete='CASCADE'),nullable=False),sa.Column('cycle',sa.Integer(),nullable=False),sa.Column('status',sa.String(32),nullable=False),
        sa.Column('goal',sa.Text(),nullable=False),sa.Column('actions_json',sa.JSON(),nullable=False),sa.Column('execution_job_ids',sa.JSON(),nullable=True),sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),sa.Column('updated_at',sa.DateTime(timezone=True),nullable=False))
    op.create_index('ix_collection_plans_case_id','collection_plans',['case_id']); op.create_index('ix_collection_plans_diagnosis_run_id','collection_plans',['diagnosis_run_id']); op.create_index('ix_collection_plans_status','collection_plans',['status'])

def downgrade():
    op.drop_table('collection_plans'); op.drop_table('hypothesis_evidence'); op.drop_table('hypotheses'); op.drop_table('diagnosis_runs')
