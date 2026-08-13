"""M6.2 reproduction intelligence mock-platform core

Revision ID: 0008
Revises: 0007
"""
from alembic import op
import sqlalchemy as sa

revision='0008'; down_revision='0007'; branch_labels=None; depends_on=None


def upgrade():
    op.create_table('reproduction_profiles',
        sa.Column('id',sa.String(36),primary_key=True),
        sa.Column('profile_key',sa.String(128),nullable=False),
        sa.Column('name',sa.String(256),nullable=False),
        sa.Column('active_version',sa.String(64),nullable=True),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),
        sa.Column('updated_at',sa.DateTime(timezone=True),nullable=False),
        sa.UniqueConstraint('profile_key'))
    op.create_index('ix_reproduction_profiles_profile_key','reproduction_profiles',['profile_key'],unique=True)

    op.create_table('reproduction_profile_versions',
        sa.Column('id',sa.String(36),primary_key=True),
        sa.Column('profile_id',sa.String(36),sa.ForeignKey('reproduction_profiles.id',ondelete='CASCADE'),nullable=False),
        sa.Column('version',sa.String(64),nullable=False),
        sa.Column('checksum',sa.String(64),nullable=False),
        sa.Column('status',sa.String(32),nullable=False),
        sa.Column('content_json',sa.JSON(),nullable=False),
        sa.Column('created_by',sa.String(128)),
        sa.Column('approved_by',sa.String(128)),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),
        sa.Column('approved_at',sa.DateTime(timezone=True)),
        sa.UniqueConstraint('profile_id','version',name='uq_reproduction_profile_version'))
    op.create_index('ix_reproduction_profile_versions_profile_id','reproduction_profile_versions',['profile_id'])
    op.create_index('ix_reproduction_profile_versions_checksum','reproduction_profile_versions',['checksum'])
    op.create_index('ix_reproduction_profile_versions_status','reproduction_profile_versions',['status'])

    op.create_table('reproduction_sessions',
        sa.Column('id',sa.String(36),primary_key=True),
        sa.Column('case_id',sa.String(36),sa.ForeignKey('cases.id',ondelete='CASCADE'),nullable=False),
        sa.Column('device_id',sa.String(36),sa.ForeignKey('case_devices.id',ondelete='RESTRICT'),nullable=False),
        sa.Column('profile_key',sa.String(128),nullable=False),
        sa.Column('profile_version',sa.String(64),nullable=False),
        sa.Column('profile_checksum',sa.String(64),nullable=False),
        sa.Column('effective_profile_snapshot',sa.JSON(),nullable=False),
        sa.Column('platform_profile_id',sa.String(128)),
        sa.Column('platform_profile_version',sa.String(64)),
        sa.Column('state',sa.String(40),nullable=False),
        sa.Column('capture_stage',sa.String(32),nullable=False),
        sa.Column('cleanup_required',sa.Boolean(),nullable=False),
        sa.Column('cleanup_status',sa.String(40),nullable=False),
        sa.Column('capture_completeness',sa.String(32),nullable=False),
        sa.Column('evidence_sufficiency',sa.String(64),nullable=False),
        sa.Column('primary_target_call_id',sa.String(36)),
        sa.Column('voice_runtime_context_json',sa.JSON()),
        sa.Column('owner_worker',sa.String(128)),
        sa.Column('lease_expires_at',sa.DateTime(timezone=True)),
        sa.Column('heartbeat_at',sa.DateTime(timezone=True)),
        sa.Column('retry_parent_session_id',sa.String(36),sa.ForeignKey('reproduction_sessions.id',ondelete='SET NULL')),
        sa.Column('terminal_reason',sa.String(128)),
        sa.Column('terminal_detail_json',sa.JSON()),
        sa.Column('started_at',sa.DateTime(timezone=True)),
        sa.Column('ended_at',sa.DateTime(timezone=True)),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),
        sa.Column('updated_at',sa.DateTime(timezone=True),nullable=False))
    for col in ['case_id','device_id','profile_key','profile_checksum','state','capture_stage','cleanup_status','capture_completeness','evidence_sufficiency','primary_target_call_id','owner_worker','lease_expires_at','retry_parent_session_id']:
        op.create_index(f'ix_reproduction_sessions_{col}','reproduction_sessions',[col])

    op.create_table('reproduction_attempts',
        sa.Column('id',sa.String(36),primary_key=True),
        sa.Column('session_id',sa.String(36),sa.ForeignKey('reproduction_sessions.id',ondelete='CASCADE'),nullable=False),
        sa.Column('case_id',sa.String(36),sa.ForeignKey('cases.id',ondelete='CASCADE'),nullable=False),
        sa.Column('attempt_no',sa.Integer(),nullable=False),
        sa.Column('status',sa.String(32),nullable=False),
        sa.Column('valid',sa.Boolean(),nullable=False),
        sa.Column('start_anchor_type',sa.String(64)),
        sa.Column('start_anchor_ms',sa.BigInteger()),
        sa.Column('end_anchor_type',sa.String(64)),
        sa.Column('end_anchor_ms',sa.BigInteger()),
        sa.Column('reconstructed_start_ms',sa.BigInteger()),
        sa.Column('details_json',sa.JSON()),
        sa.Column('started_at',sa.DateTime(timezone=True),nullable=False),
        sa.Column('ended_at',sa.DateTime(timezone=True)),
        sa.UniqueConstraint('session_id','attempt_no',name='uq_reproduction_attempt_no'))
    op.create_index('ix_reproduction_attempts_session_id','reproduction_attempts',['session_id'])
    op.create_index('ix_reproduction_attempts_case_id','reproduction_attempts',['case_id'])
    op.create_index('ix_reproduction_attempts_status','reproduction_attempts',['status'])

    op.create_table('reproduction_calls',
        sa.Column('id',sa.String(36),primary_key=True),
        sa.Column('session_id',sa.String(36),sa.ForeignKey('reproduction_sessions.id',ondelete='CASCADE'),nullable=False),
        sa.Column('attempt_id',sa.String(36),sa.ForeignKey('reproduction_attempts.id',ondelete='SET NULL')),
        sa.Column('case_id',sa.String(36),sa.ForeignKey('cases.id',ondelete='CASCADE'),nullable=False),
        sa.Column('call_no',sa.Integer(),nullable=False),
        sa.Column('external_call_ref',sa.String(128)),
        sa.Column('status',sa.String(32),nullable=False),
        sa.Column('verdict',sa.String(32)),
        sa.Column('role',sa.String(32)),
        sa.Column('live_summary_json',sa.JSON()),
        sa.Column('quick_analysis_json',sa.JSON()),
        sa.Column('started_at',sa.DateTime(timezone=True),nullable=False),
        sa.Column('ended_at',sa.DateTime(timezone=True)),
        sa.UniqueConstraint('session_id','call_no',name='uq_reproduction_call_no'))
    for col in ['session_id','attempt_id','case_id','external_call_ref','status','verdict','role']:
        op.create_index(f'ix_reproduction_calls_{col}','reproduction_calls',[col])

    op.create_table('reproduction_events',
        sa.Column('id',sa.String(36),primary_key=True),
        sa.Column('session_id',sa.String(36),sa.ForeignKey('reproduction_sessions.id',ondelete='CASCADE'),nullable=False),
        sa.Column('attempt_id',sa.String(36),sa.ForeignKey('reproduction_attempts.id',ondelete='SET NULL')),
        sa.Column('call_id',sa.String(36),sa.ForeignKey('reproduction_calls.id',ondelete='SET NULL')),
        sa.Column('case_id',sa.String(36),sa.ForeignKey('cases.id',ondelete='CASCADE'),nullable=False),
        sa.Column('event_type',sa.String(128),nullable=False),
        sa.Column('source',sa.String(64),nullable=False),
        sa.Column('anchor_type',sa.String(64)),
        sa.Column('session_relative_ms',sa.BigInteger()),
        sa.Column('source_timestamp',sa.String(128)),
        sa.Column('timestamp_source',sa.String(64)),
        sa.Column('uncertainty_ms',sa.Integer()),
        sa.Column('payload_json',sa.JSON()),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False))
    for col in ['session_id','attempt_id','call_id','case_id','event_type','source','anchor_type','session_relative_ms','created_at']:
        op.create_index(f'ix_reproduction_events_{col}','reproduction_events',[col])

    op.create_table('voice_runtime_context_snapshots',
        sa.Column('id',sa.String(36),primary_key=True),
        sa.Column('session_id',sa.String(36),sa.ForeignKey('reproduction_sessions.id',ondelete='CASCADE'),nullable=False),
        sa.Column('case_id',sa.String(36),sa.ForeignKey('cases.id',ondelete='CASCADE'),nullable=False),
        sa.Column('voice_vlan_id',sa.String(64)),
        sa.Column('voice_interface',sa.String(128)),
        sa.Column('voice_device_ip',sa.String(128)),
        sa.Column('voice_gateway_ip',sa.String(128)),
        sa.Column('interface_up',sa.Boolean(),nullable=False),
        sa.Column('resolver_id',sa.String(128),nullable=False),
        sa.Column('resolver_version',sa.String(64),nullable=False),
        sa.Column('snapshot_json',sa.JSON(),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),
        sa.UniqueConstraint('session_id',name='uq_voice_context_session'))
    op.create_index('ix_voice_runtime_context_snapshots_session_id','voice_runtime_context_snapshots',['session_id'])
    op.create_index('ix_voice_runtime_context_snapshots_case_id','voice_runtime_context_snapshots',['case_id'])

    op.create_table('arm_validation_results',
        sa.Column('id',sa.String(36),primary_key=True),
        sa.Column('session_id',sa.String(36),sa.ForeignKey('reproduction_sessions.id',ondelete='CASCADE'),nullable=False),
        sa.Column('validation_no',sa.Integer(),nullable=False),
        sa.Column('status',sa.String(32),nullable=False),
        sa.Column('required_channels_json',sa.JSON(),nullable=False),
        sa.Column('observed_channels_json',sa.JSON(),nullable=False),
        sa.Column('failed_reasons_json',sa.JSON()),
        sa.Column('started_at',sa.DateTime(timezone=True),nullable=False),
        sa.Column('finished_at',sa.DateTime(timezone=True)))
    op.create_index('ix_arm_validation_results_session_id','arm_validation_results',['session_id'])
    op.create_index('ix_arm_validation_results_status','arm_validation_results',['status'])

    op.create_table('capture_channel_health',
        sa.Column('id',sa.String(36),primary_key=True),
        sa.Column('session_id',sa.String(36),sa.ForeignKey('reproduction_sessions.id',ondelete='CASCADE'),nullable=False),
        sa.Column('channel',sa.String(32),nullable=False),
        sa.Column('status',sa.String(32),nullable=False),
        sa.Column('packet_count',sa.BigInteger(),nullable=False),
        sa.Column('last_observed_at',sa.DateTime(timezone=True)),
        sa.Column('health_json',sa.JSON()),
        sa.Column('updated_at',sa.DateTime(timezone=True),nullable=False),
        sa.UniqueConstraint('session_id','channel',name='uq_capture_channel_health'))
    op.create_index('ix_capture_channel_health_session_id','capture_channel_health',['session_id'])
    op.create_index('ix_capture_channel_health_channel','capture_channel_health',['channel'])
    op.create_index('ix_capture_channel_health_status','capture_channel_health',['status'])

    op.create_table('device_diagnostic_locks',
        sa.Column('id',sa.String(36),primary_key=True),
        sa.Column('device_id',sa.String(36),sa.ForeignKey('case_devices.id',ondelete='CASCADE'),nullable=False),
        sa.Column('session_id',sa.String(36),sa.ForeignKey('reproduction_sessions.id',ondelete='CASCADE'),nullable=False),
        sa.Column('owner_worker',sa.String(128),nullable=False),
        sa.Column('status',sa.String(32),nullable=False),
        sa.Column('acquired_at',sa.DateTime(timezone=True),nullable=False),
        sa.Column('heartbeat_at',sa.DateTime(timezone=True),nullable=False),
        sa.Column('lease_expires_at',sa.DateTime(timezone=True),nullable=False),
        sa.Column('released_at',sa.DateTime(timezone=True)),
        sa.UniqueConstraint('device_id',name='uq_device_diagnostic_lock_device'),
        sa.UniqueConstraint('session_id'))
    for col in ['device_id','session_id','owner_worker','status','lease_expires_at']:
        op.create_index(f'ix_device_diagnostic_locks_{col}','device_diagnostic_locks',[col])

    op.create_table('cleanup_runs',
        sa.Column('id',sa.String(36),primary_key=True),
        sa.Column('session_id',sa.String(36),sa.ForeignKey('reproduction_sessions.id',ondelete='CASCADE'),nullable=False),
        sa.Column('run_no',sa.Integer(),nullable=False),
        sa.Column('status',sa.String(32),nullable=False),
        sa.Column('action_results_json',sa.JSON()),
        sa.Column('validation_json',sa.JSON()),
        sa.Column('error_code',sa.String(128)),
        sa.Column('started_at',sa.DateTime(timezone=True)),
        sa.Column('finished_at',sa.DateTime(timezone=True)),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False))
    op.create_index('ix_cleanup_runs_session_id','cleanup_runs',['session_id'])
    op.create_index('ix_cleanup_runs_status','cleanup_runs',['status'])

    op.create_table('diagnostic_questions',
        sa.Column('id',sa.String(36),primary_key=True),
        sa.Column('case_id',sa.String(36),sa.ForeignKey('cases.id',ondelete='CASCADE'),nullable=False),
        sa.Column('session_id',sa.String(36),sa.ForeignKey('reproduction_sessions.id',ondelete='SET NULL')),
        sa.Column('question_key',sa.String(128),nullable=False),
        sa.Column('template_version',sa.String(64),nullable=False),
        sa.Column('state',sa.String(32),nullable=False),
        sa.Column('level',sa.String(32),nullable=False),
        sa.Column('requirements_json',sa.JSON(),nullable=False),
        sa.Column('answer_json',sa.JSON()),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),
        sa.Column('updated_at',sa.DateTime(timezone=True),nullable=False))
    op.create_index('ix_diagnostic_questions_case_id','diagnostic_questions',['case_id'])
    op.create_index('ix_diagnostic_questions_session_id','diagnostic_questions',['session_id'])
    op.create_index('ix_diagnostic_questions_question_key','diagnostic_questions',['question_key'])
    op.create_index('ix_diagnostic_questions_state','diagnostic_questions',['state'])


def downgrade():
    for table in [
        'diagnostic_questions','cleanup_runs','device_diagnostic_locks','capture_channel_health',
        'arm_validation_results','voice_runtime_context_snapshots','reproduction_events',
        'reproduction_calls','reproduction_attempts','reproduction_sessions',
        'reproduction_profile_versions','reproduction_profiles']:
        op.drop_table(table)
