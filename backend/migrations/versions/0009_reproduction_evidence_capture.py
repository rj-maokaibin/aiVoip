"""phase c2 reproduction evidence capture

Revision ID: 0009_reproduction_evidence_capture
Revises: 0008_reproduction_core
"""
from alembic import op
import sqlalchemy as sa

revision='0009_reproduction_evidence_capture'
down_revision='0008'
branch_labels=None
depends_on=None


def upgrade():
    # 0009's revision identifier is longer than the legacy VARCHAR(32) created in 0001.
    # Alembic writes this identifier after upgrade(), so expand its metadata column first.
    op.alter_column('alembic_version', 'version_num', existing_type=sa.String(32), type_=sa.String(128), existing_nullable=False)
    op.create_table('reproduction_capture_states',
        sa.Column('id',sa.String(36),primary_key=True),
        sa.Column('session_id',sa.String(36),sa.ForeignKey('reproduction_sessions.id',ondelete='CASCADE'),nullable=False),
        sa.Column('pretrigger_ms',sa.BigInteger(),nullable=False),sa.Column('segment_ms',sa.BigInteger(),nullable=False),
        sa.Column('preserve_mode',sa.Boolean(),nullable=False),sa.Column('freeze_anchor_ms',sa.BigInteger()),
        sa.Column('total_bytes',sa.BigInteger(),nullable=False),sa.Column('finalized',sa.Boolean(),nullable=False),
        sa.Column('manifest_json',sa.JSON()),sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),
        sa.Column('updated_at',sa.DateTime(timezone=True),nullable=False),sa.UniqueConstraint('session_id',name='uq_reproduction_capture_state_session'))
    op.create_index('ix_reproduction_capture_states_session_id','reproduction_capture_states',['session_id'])

    op.create_table('reproduction_capture_segments',
        sa.Column('id',sa.String(36),primary_key=True),
        sa.Column('session_id',sa.String(36),sa.ForeignKey('reproduction_sessions.id',ondelete='CASCADE'),nullable=False),
        sa.Column('attempt_id',sa.String(36),sa.ForeignKey('reproduction_attempts.id',ondelete='SET NULL')),
        sa.Column('call_id',sa.String(36),sa.ForeignKey('reproduction_calls.id',ondelete='SET NULL')),
        sa.Column('evidence_id',sa.String(36),sa.ForeignKey('evidences.id',ondelete='SET NULL')),
        sa.Column('channel',sa.String(32),nullable=False),sa.Column('segment_no',sa.Integer(),nullable=False),
        sa.Column('start_ms',sa.BigInteger(),nullable=False),sa.Column('end_ms',sa.BigInteger(),nullable=False),
        sa.Column('local_path',sa.String(1024),nullable=False),sa.Column('content_type',sa.String(128),nullable=False),
        sa.Column('size_bytes',sa.BigInteger(),nullable=False),sa.Column('sha256',sa.String(64),nullable=False),
        sa.Column('status',sa.String(32),nullable=False),sa.Column('frozen',sa.Boolean(),nullable=False),
        sa.Column('retained',sa.Boolean(),nullable=False),sa.Column('retention_class',sa.String(32),nullable=False),
        sa.Column('metadata_json',sa.JSON()),sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),
        sa.UniqueConstraint('session_id','channel','segment_no',name='uq_reproduction_capture_segment_no'))
    for c in ['session_id','attempt_id','call_id','evidence_id','channel','start_ms','end_ms','sha256','status','retention_class']:
        op.create_index(f'ix_reproduction_capture_segments_{c}','reproduction_capture_segments',[c])

    op.create_table('evidence_finalize_runs',
        sa.Column('id',sa.String(36),primary_key=True),sa.Column('session_id',sa.String(36),sa.ForeignKey('reproduction_sessions.id',ondelete='CASCADE'),nullable=False),
        sa.Column('run_no',sa.Integer(),nullable=False),sa.Column('status',sa.String(32),nullable=False),
        sa.Column('evidence_ids_json',sa.JSON()),sa.Column('manifest_object_key',sa.String(1024)),sa.Column('manifest_sha256',sa.String(64)),
        sa.Column('error_code',sa.String(128)),sa.Column('error_message',sa.Text()),
        sa.Column('started_at',sa.DateTime(timezone=True)),sa.Column('finished_at',sa.DateTime(timezone=True)),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),sa.UniqueConstraint('session_id','run_no',name='uq_evidence_finalize_run_no'))
    op.create_index('ix_evidence_finalize_runs_session_id','evidence_finalize_runs',['session_id'])
    op.create_index('ix_evidence_finalize_runs_status','evidence_finalize_runs',['status'])


def downgrade():
    op.drop_table('evidence_finalize_runs')
    op.drop_table('reproduction_capture_segments')
    op.drop_table('reproduction_capture_states')
