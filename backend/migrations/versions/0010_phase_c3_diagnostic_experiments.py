"""phase c3 diagnostic question dag experiments causal fix verification

Revision ID: 0010_phase_c3_diagnostic_experiments
Revises: 0009_reproduction_evidence_capture
"""
from alembic import op
import sqlalchemy as sa

revision='0010_phase_c3_diagnostic_experiments'
down_revision='0009_reproduction_evidence_capture'
branch_labels=None
depends_on=None


def upgrade():
    op.add_column('diagnostic_questions',sa.Column('parent_question_id',sa.String(36),nullable=True))
    op.add_column('diagnostic_questions',sa.Column('template_checksum',sa.String(64),nullable=True))
    op.add_column('diagnostic_questions',sa.Column('priority',sa.Integer(),nullable=False,server_default='100'))
    op.add_column('diagnostic_questions',sa.Column('information_gain',sa.Integer(),nullable=False,server_default='1000'))
    op.add_column('diagnostic_questions',sa.Column('selected_reason',sa.Text(),nullable=True))
    op.add_column('diagnostic_questions',sa.Column('evidence_refs_json',sa.JSON(),nullable=True))
    op.create_foreign_key('fk_diagnostic_questions_parent','diagnostic_questions','diagnostic_questions',['parent_question_id'],['id'],ondelete='SET NULL')
    op.create_index('ix_diagnostic_questions_parent_question_id','diagnostic_questions',['parent_question_id'])
    op.create_index('ix_diagnostic_questions_template_checksum','diagnostic_questions',['template_checksum'])
    op.create_index('ix_diagnostic_questions_level','diagnostic_questions',['level'])

    op.create_table('diagnostic_experiments',
        sa.Column('id',sa.String(36),primary_key=True),
        sa.Column('case_id',sa.String(36),sa.ForeignKey('cases.id',ondelete='CASCADE'),nullable=False),
        sa.Column('hypothesis_id',sa.String(36),sa.ForeignKey('hypotheses.id',ondelete='SET NULL')),
        sa.Column('question_id',sa.String(36),sa.ForeignKey('diagnostic_questions.id',ondelete='SET NULL')),
        sa.Column('profile_key',sa.String(128),nullable=False),sa.Column('profile_version',sa.String(64),nullable=False),
        sa.Column('profile_checksum',sa.String(64),nullable=False),sa.Column('effective_profile_snapshot',sa.JSON(),nullable=False),
        sa.Column('state',sa.String(40),nullable=False),sa.Column('confirmation_policy',sa.String(40),nullable=False),
        sa.Column('independent_variable',sa.String(128),nullable=False),sa.Column('target_finding',sa.String(128),nullable=False),
        sa.Column('reproduction_profile_id',sa.String(128),nullable=False),sa.Column('current_round',sa.Integer(),nullable=False),
        sa.Column('causal_state',sa.String(40),nullable=False),sa.Column('terminal_reason',sa.String(128)),
        sa.Column('created_by',sa.String(128)),sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),
        sa.Column('updated_at',sa.DateTime(timezone=True),nullable=False))
    for c in ['case_id','hypothesis_id','question_id','profile_key','profile_checksum','state','confirmation_policy','independent_variable','target_finding','reproduction_profile_id','causal_state']:
        op.create_index(f'ix_diagnostic_experiments_{c}','diagnostic_experiments',[c])

    op.create_table('experiment_runs',
        sa.Column('id',sa.String(36),primary_key=True),
        sa.Column('experiment_id',sa.String(36),sa.ForeignKey('diagnostic_experiments.id',ondelete='CASCADE'),nullable=False),
        sa.Column('case_id',sa.String(36),sa.ForeignKey('cases.id',ondelete='CASCADE'),nullable=False),
        sa.Column('run_no',sa.Integer(),nullable=False),sa.Column('variant',sa.String(16),nullable=False),
        sa.Column('status',sa.String(40),nullable=False),
        sa.Column('reproduction_session_id',sa.String(36),sa.ForeignKey('reproduction_sessions.id',ondelete='SET NULL')),
        sa.Column('reproduction_call_id',sa.String(36),sa.ForeignKey('reproduction_calls.id',ondelete='SET NULL')),
        sa.Column('target_verdict',sa.String(32)),sa.Column('target_finding_present',sa.Boolean()),sa.Column('metrics_json',sa.JSON()),
        sa.Column('external_action_required',sa.Boolean(),nullable=False),sa.Column('external_action_completed_at',sa.DateTime(timezone=True)),
        sa.Column('started_at',sa.DateTime(timezone=True)),sa.Column('finished_at',sa.DateTime(timezone=True)),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),
        sa.UniqueConstraint('experiment_id','run_no',name='uq_experiment_run_no'))
    for c in ['experiment_id','case_id','variant','status','reproduction_session_id','reproduction_call_id','target_verdict']:
        op.create_index(f'ix_experiment_runs_{c}','experiment_runs',[c])

    op.create_table('experiment_environment_snapshots',
        sa.Column('id',sa.String(36),primary_key=True),
        sa.Column('experiment_id',sa.String(36),sa.ForeignKey('diagnostic_experiments.id',ondelete='CASCADE'),nullable=False),
        sa.Column('run_id',sa.String(36),sa.ForeignKey('experiment_runs.id',ondelete='CASCADE'),nullable=False),
        sa.Column('case_id',sa.String(36),sa.ForeignKey('cases.id',ondelete='CASCADE'),nullable=False),
        sa.Column('phase',sa.String(16),nullable=False),sa.Column('snapshot_json',sa.JSON(),nullable=False),
        sa.Column('checksum',sa.String(64),nullable=False),sa.Column('created_at',sa.DateTime(timezone=True),nullable=False))
    for c in ['experiment_id','run_id','case_id','phase','checksum']:
        op.create_index(f'ix_experiment_environment_snapshots_{c}','experiment_environment_snapshots',[c])

    op.create_table('environment_comparisons',
        sa.Column('id',sa.String(36),primary_key=True),
        sa.Column('experiment_id',sa.String(36),sa.ForeignKey('diagnostic_experiments.id',ondelete='CASCADE'),nullable=False),
        sa.Column('baseline_run_id',sa.String(36),sa.ForeignKey('experiment_runs.id',ondelete='CASCADE'),nullable=False),
        sa.Column('variant_run_id',sa.String(36),sa.ForeignKey('experiment_runs.id',ondelete='CASCADE'),nullable=False),
        sa.Column('status',sa.String(40),nullable=False),sa.Column('expected_changes_json',sa.JSON()),
        sa.Column('soft_drift_json',sa.JSON()),sa.Column('hard_drift_json',sa.JSON()),sa.Column('compared_fields_json',sa.JSON()),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False))
    for c in ['experiment_id','baseline_run_id','variant_run_id','status']:
        op.create_index(f'ix_environment_comparisons_{c}','environment_comparisons',[c])

    op.create_table('causal_assessments',
        sa.Column('id',sa.String(36),primary_key=True),
        sa.Column('experiment_id',sa.String(36),sa.ForeignKey('diagnostic_experiments.id',ondelete='CASCADE'),nullable=False),
        sa.Column('case_id',sa.String(36),sa.ForeignKey('cases.id',ondelete='CASCADE'),nullable=False),
        sa.Column('hypothesis_id',sa.String(36),sa.ForeignKey('hypotheses.id',ondelete='SET NULL')),
        sa.Column('state',sa.String(40),nullable=False),sa.Column('confirmation_policy',sa.String(40),nullable=False),
        sa.Column('supporting_run_ids_json',sa.JSON(),nullable=False),sa.Column('environment_comparison_ids_json',sa.JSON(),nullable=False),
        sa.Column('hard_contradictions_json',sa.JSON()),sa.Column('rationale_json',sa.JSON(),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False))
    for c in ['experiment_id','case_id','hypothesis_id','state','confirmation_policy']:
        op.create_index(f'ix_causal_assessments_{c}','causal_assessments',[c])

    op.create_table('fix_actions',
        sa.Column('id',sa.String(36),primary_key=True),sa.Column('case_id',sa.String(36),sa.ForeignKey('cases.id',ondelete='CASCADE'),nullable=False),
        sa.Column('hypothesis_id',sa.String(36),sa.ForeignKey('hypotheses.id',ondelete='SET NULL')),
        sa.Column('experiment_id',sa.String(36),sa.ForeignKey('diagnostic_experiments.id',ondelete='SET NULL')),
        sa.Column('action_type',sa.String(64),nullable=False),sa.Column('description',sa.Text(),nullable=False),
        sa.Column('version_before',sa.String(128)),sa.Column('version_after',sa.String(128)),sa.Column('actor',sa.String(128)),
        sa.Column('metadata_json',sa.JSON()),sa.Column('created_at',sa.DateTime(timezone=True),nullable=False))
    for c in ['case_id','hypothesis_id','experiment_id','action_type']:
        op.create_index(f'ix_fix_actions_{c}','fix_actions',[c])

    op.create_table('fix_verification_runs',
        sa.Column('id',sa.String(36),primary_key=True),sa.Column('case_id',sa.String(36),sa.ForeignKey('cases.id',ondelete='CASCADE'),nullable=False),
        sa.Column('fix_action_id',sa.String(36),sa.ForeignKey('fix_actions.id',ondelete='CASCADE'),nullable=False),
        sa.Column('baseline_session_id',sa.String(36),sa.ForeignKey('reproduction_sessions.id',ondelete='SET NULL')),
        sa.Column('verification_session_id',sa.String(36),sa.ForeignKey('reproduction_sessions.id',ondelete='SET NULL')),
        sa.Column('baseline_call_id',sa.String(36),sa.ForeignKey('reproduction_calls.id',ondelete='SET NULL')),
        sa.Column('verification_call_id',sa.String(36),sa.ForeignKey('reproduction_calls.id',ondelete='SET NULL')),
        sa.Column('reproduction_profile_id',sa.String(128),nullable=False),sa.Column('target_finding',sa.String(128),nullable=False),
        sa.Column('required_calls',sa.Integer(),nullable=False),sa.Column('max_calls',sa.Integer(),nullable=False),
        sa.Column('verification_call_count',sa.Integer(),nullable=False,server_default='0'),
        sa.Column('successful_call_count',sa.Integer(),nullable=False,server_default='0'),sa.Column('evaluations_json',sa.JSON()),
        sa.Column('status',sa.String(40),nullable=False),sa.Column('environment_status',sa.String(40)),
        sa.Column('business_checks_json',sa.JSON()),sa.Column('comparison_json',sa.JSON()),
        sa.Column('evidence_id',sa.String(36),sa.ForeignKey('evidences.id',ondelete='SET NULL')),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),sa.Column('updated_at',sa.DateTime(timezone=True),nullable=False))
    for c in ['case_id','fix_action_id','baseline_session_id','verification_session_id','baseline_call_id','verification_call_id','reproduction_profile_id','target_finding','status','evidence_id']:
        op.create_index(f'ix_fix_verification_runs_{c}','fix_verification_runs',[c])


def downgrade():
    for table in ['fix_verification_runs','fix_actions','causal_assessments','environment_comparisons','experiment_environment_snapshots','experiment_runs','diagnostic_experiments']:
        op.drop_table(table)
    op.drop_index('ix_diagnostic_questions_level',table_name='diagnostic_questions')
    op.drop_index('ix_diagnostic_questions_template_checksum',table_name='diagnostic_questions')
    op.drop_index('ix_diagnostic_questions_parent_question_id',table_name='diagnostic_questions')
    op.drop_constraint('fk_diagnostic_questions_parent','diagnostic_questions',type_='foreignkey')
    for col in ['evidence_refs_json','selected_reason','information_gain','priority','template_checksum','parent_question_id']:
        op.drop_column('diagnostic_questions',col)
