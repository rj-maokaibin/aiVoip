"""V1.0 engineering contract normalization (Phase A2)

Revision ID: 0007
Revises: 0006
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision='0007'; down_revision='0006'; branch_labels=None; depends_on=None


def upgrade():
    # EC-01: richer append-only Case transition history.
    op.add_column('case_state_history', sa.Column('event', sa.String(64), nullable=True))
    op.add_column('case_state_history', sa.Column('actor', sa.String(128), nullable=True))
    op.add_column('case_state_history', sa.Column('context_json', sa.JSON(), nullable=True))
    op.create_index('ix_case_state_history_to_status','case_state_history',['to_status'])
    op.create_index('ix_case_state_history_event','case_state_history',['event'])

    # EC-01: Job transitions must also be append-only/auditable.
    op.create_table('job_state_history',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('job_id', sa.String(36), sa.ForeignKey('jobs.id', ondelete='CASCADE'), nullable=False),
        sa.Column('case_id', sa.String(36), sa.ForeignKey('cases.id', ondelete='CASCADE'), nullable=False),
        sa.Column('from_status', sa.String(32), nullable=True),
        sa.Column('to_status', sa.String(32), nullable=False),
        sa.Column('actor', sa.String(128), nullable=True),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('detail_json', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_job_state_history_job_id','job_state_history',['job_id'])
    op.create_index('ix_job_state_history_case_id','job_state_history',['case_id'])
    op.create_index('ix_job_state_history_to_status','job_state_history',['to_status'])

    # EC-13: normalize Audit contract while preserving legacy detail.
    for col in [
        sa.Column('actor_type', sa.String(32), nullable=True),
        sa.Column('action', sa.String(128), nullable=True),
        sa.Column('before_json', sa.JSON(), nullable=True),
        sa.Column('after_json', sa.JSON(), nullable=True),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('trace_id', sa.String(128), nullable=True),
    ]:
        op.add_column('audit_logs', col)
    for name, cols in [
        ('ix_audit_logs_actor',['actor']),
        ('ix_audit_logs_actor_type',['actor_type']),
        ('ix_audit_logs_event_type',['event_type']),
        ('ix_audit_logs_action',['action']),
        ('ix_audit_logs_target_type',['target_type']),
        ('ix_audit_logs_target_id',['target_id']),
        ('ix_audit_logs_trace_id',['trace_id']),
        ('ix_audit_logs_created_at',['created_at']),
    ]:
        op.create_index(name,'audit_logs',cols)


def downgrade():
    for name in [
        'ix_audit_logs_created_at','ix_audit_logs_trace_id','ix_audit_logs_target_id',
        'ix_audit_logs_target_type','ix_audit_logs_action','ix_audit_logs_event_type',
        'ix_audit_logs_actor_type','ix_audit_logs_actor',
    ]:
        op.drop_index(name, table_name='audit_logs')
    for col in ['trace_id','reason','after_json','before_json','action','actor_type']:
        op.drop_column('audit_logs', col)

    op.drop_table('job_state_history')
    op.drop_index('ix_case_state_history_event', table_name='case_state_history')
    op.drop_index('ix_case_state_history_to_status', table_name='case_state_history')
    for col in ['context_json','actor','event']:
        op.drop_column('case_state_history', col)
