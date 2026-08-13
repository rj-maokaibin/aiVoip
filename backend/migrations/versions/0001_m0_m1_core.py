"""m0 m1 core
Revision ID: 0001
Revises:
"""
from alembic import op
import sqlalchemy as sa
revision='0001'; down_revision=None; branch_labels=None; depends_on=None

def upgrade():
    op.create_table('cases',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('case_no', sa.String(64), nullable=False, unique=True),
        sa.Column('summary', sa.Text(), nullable=False),
        sa.Column('status', sa.String(32), nullable=False, index=True),
        sa.Column('created_by', sa.String(128), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False))
    op.create_table('case_devices',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('case_id', sa.String(36), sa.ForeignKey('cases.id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('ip', sa.String(128), nullable=False),
        sa.Column('ssh_port', sa.Integer(), nullable=False),
        sa.Column('sn', sa.String(128), nullable=False),
        sa.Column('username', sa.String(64), nullable=False),
        sa.Column('platform_id', sa.String(128), nullable=True),
        sa.Column('device_info', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False))
    op.create_table('case_state_history',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('case_id', sa.String(36), sa.ForeignKey('cases.id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('from_status', sa.String(32), nullable=True),
        sa.Column('to_status', sa.String(32), nullable=False),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False))
    op.create_table('jobs',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('case_id', sa.String(36), sa.ForeignKey('cases.id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('type', sa.String(64), nullable=False),
        sa.Column('status', sa.String(32), nullable=False, index=True),
        sa.Column('profile_id', sa.String(128), nullable=True),
        sa.Column('error_code', sa.String(128), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True))
    op.create_table('action_runs',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('case_id', sa.String(36), sa.ForeignKey('cases.id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('job_id', sa.String(36), sa.ForeignKey('jobs.id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('device_id', sa.String(36), sa.ForeignKey('case_devices.id', ondelete='CASCADE'), nullable=False),
        sa.Column('action_id', sa.String(128), nullable=False),
        sa.Column('risk_level', sa.String(8), nullable=False),
        sa.Column('status', sa.String(32), nullable=False),
        sa.Column('exit_status', sa.Integer(), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True))
    op.create_table('evidences',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('case_id', sa.String(36), sa.ForeignKey('cases.id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('device_id', sa.String(36), sa.ForeignKey('case_devices.id', ondelete='SET NULL'), nullable=True),
        sa.Column('job_id', sa.String(36), sa.ForeignKey('jobs.id', ondelete='SET NULL'), nullable=True),
        sa.Column('action_run_id', sa.String(36), sa.ForeignKey('action_runs.id', ondelete='SET NULL'), nullable=True),
        sa.Column('type', sa.String(64), nullable=False),
        sa.Column('source', sa.String(64), nullable=False),
        sa.Column('filename', sa.String(512), nullable=False),
        sa.Column('object_key', sa.String(1024), nullable=False),
        sa.Column('size_bytes', sa.BigInteger(), nullable=False),
        sa.Column('sha256', sa.String(64), nullable=False, index=True),
        sa.Column('content_type', sa.String(128), nullable=True),
        sa.Column('metadata_json', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False))
    op.create_table('audit_logs',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('case_id', sa.String(36), nullable=True, index=True),
        sa.Column('actor', sa.String(128), nullable=True),
        sa.Column('event_type', sa.String(128), nullable=False),
        sa.Column('target_type', sa.String(64), nullable=True),
        sa.Column('target_id', sa.String(128), nullable=True),
        sa.Column('detail', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False))

def downgrade():
    for t in ['audit_logs','evidences','action_runs','jobs','case_state_history','case_devices','cases']:
        op.drop_table(t)
