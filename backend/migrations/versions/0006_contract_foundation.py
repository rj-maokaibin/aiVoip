"""V1.0 engineering contract foundation

Revision ID: 0006
Revises: 0005
"""
from __future__ import annotations

import uuid

from alembic import op
import sqlalchemy as sa

revision='0006'; down_revision='0005'; branch_labels=None; depends_on=None


def _drop_fk_for_columns(table:str, columns:list[str]):
    bind=op.get_bind(); inspector=sa.inspect(bind)
    for fk in inspector.get_foreign_keys(table):
        if list(fk.get('constrained_columns') or [])==columns and fk.get('name'):
            op.drop_constraint(fk['name'],table,type_='foreignkey')
            return


def upgrade():
    # EC-00/01: Raw Evidence cannot disappear via Case cascade.
    _drop_fk_for_columns('evidences',['case_id'])
    op.create_foreign_key('fk_evidences_case_restrict','evidences','cases',['case_id'],['id'],ondelete='RESTRICT')

    for col in [
        sa.Column('kind',sa.String(16),nullable=False,server_default='RAW'),
        sa.Column('source_scope',sa.String(32),nullable=False,server_default='CASE'),
        sa.Column('level',sa.String(8),nullable=False,server_default='L1'),
        sa.Column('completeness',sa.String(32),nullable=False,server_default='COMPLETE'),
        sa.Column('captured_at',sa.DateTime(timezone=True),nullable=True),
        sa.Column('time_range_start',sa.DateTime(timezone=True),nullable=True),
        sa.Column('time_range_end',sa.DateTime(timezone=True),nullable=True),
        sa.Column('producer_type',sa.String(64),nullable=True),
        sa.Column('producer_id',sa.String(128),nullable=True),
        sa.Column('producer_version',sa.String(64),nullable=True),
        sa.Column('session_id',sa.String(36),nullable=True),
        sa.Column('attempt_id',sa.String(36),nullable=True),
        sa.Column('call_id',sa.String(36),nullable=True),
    ]: op.add_column('evidences',col)
    op.execute(sa.text('UPDATE evidences SET captured_at=created_at WHERE captured_at IS NULL'))
    for name,cols in [
        ('ix_evidences_type',['type']),('ix_evidences_kind',['kind']),('ix_evidences_source_scope',['source_scope']),
        ('ix_evidences_level',['level']),('ix_evidences_completeness',['completeness']),('ix_evidences_producer_id',['producer_id']),
        ('ix_evidences_session_id',['session_id']),('ix_evidences_attempt_id',['attempt_id']),('ix_evidences_call_id',['call_id']),
    ]: op.create_index(name,'evidences',cols)

    op.create_table('evidence_relations',
        sa.Column('id',sa.String(36),primary_key=True),
        sa.Column('parent_evidence_id',sa.String(36),sa.ForeignKey('evidences.id',ondelete='RESTRICT'),nullable=False),
        sa.Column('child_evidence_id',sa.String(36),sa.ForeignKey('evidences.id',ondelete='RESTRICT'),nullable=False),
        sa.Column('relation_type',sa.String(32),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),
        sa.UniqueConstraint('parent_evidence_id','child_evidence_id','relation_type',name='uq_evidence_relation'))
    op.create_index('ix_evidence_relations_parent_evidence_id','evidence_relations',['parent_evidence_id'])
    op.create_index('ix_evidence_relations_child_evidence_id','evidence_relations',['child_evidence_id'])
    op.create_index('ix_evidence_relations_relation_type','evidence_relations',['relation_type'])

    # AnalyzerRun audit contract.
    op.add_column('analyzer_runs',sa.Column('config_checksum',sa.String(64),nullable=True))
    op.add_column('analyzer_runs',sa.Column('config_snapshot',sa.JSON(),nullable=True))
    op.add_column('analyzer_runs',sa.Column('scope',sa.String(32),nullable=False,server_default='CASE'))
    op.add_column('analyzer_runs',sa.Column('output_evidence_ids',sa.JSON(),nullable=True))
    op.create_index('ix_analyzer_runs_config_checksum','analyzer_runs',['config_checksum'])
    op.create_index('ix_analyzer_runs_scope','analyzer_runs',['scope'])

    # Append-only hypothesis revision history.
    op.add_column('hypotheses',sa.Column('current_revision_id',sa.String(36),nullable=True))
    op.create_index('ix_hypotheses_current_revision_id','hypotheses',['current_revision_id'])
    op.create_table('hypothesis_revisions',
        sa.Column('id',sa.String(36),primary_key=True),
        sa.Column('hypothesis_id',sa.String(36),sa.ForeignKey('hypotheses.id',ondelete='CASCADE'),nullable=False),
        sa.Column('diagnosis_run_id',sa.String(36),sa.ForeignKey('diagnosis_runs.id',ondelete='SET NULL'),nullable=True),
        sa.Column('revision_no',sa.Integer(),nullable=False),sa.Column('supersedes_revision_id',sa.String(36),sa.ForeignKey('hypothesis_revisions.id',ondelete='RESTRICT'),nullable=True),
        sa.Column('title',sa.Text(),nullable=False),sa.Column('fault_domain',sa.String(128),nullable=False),
        sa.Column('status',sa.String(32),nullable=False),sa.Column('confidence',sa.Integer(),nullable=False),
        sa.Column('rationale',sa.Text(),nullable=True),sa.Column('confirmable',sa.Integer(),nullable=False),
        sa.Column('confirm_rule',sa.String(128),nullable=True),sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),
        sa.UniqueConstraint('hypothesis_id','revision_no',name='uq_hypothesis_revision_no'))
    for name,cols in [
        ('ix_hypothesis_revisions_hypothesis_id',['hypothesis_id']),('ix_hypothesis_revisions_diagnosis_run_id',['diagnosis_run_id']),
        ('ix_hypothesis_revisions_supersedes_revision_id',['supersedes_revision_id']),('ix_hypothesis_revisions_fault_domain',['fault_domain']),
        ('ix_hypothesis_revisions_status',['status']),
    ]: op.create_index(name,'hypothesis_revisions',cols)
    op.add_column('hypothesis_evidence',sa.Column('hypothesis_revision_id',sa.String(36),nullable=True))
    op.create_index('ix_hypothesis_evidence_hypothesis_revision_id','hypothesis_evidence',['hypothesis_revision_id'])
    op.create_foreign_key('fk_hypothesis_evidence_revision','hypothesis_evidence','hypothesis_revisions',['hypothesis_revision_id'],['id'],ondelete='CASCADE')

    bind=op.get_bind()
    status_map={'PROPOSED':'OPEN','ACTIVE':'OPEN','UNRESOLVED':'OPEN','WEAKENED':'CONTRADICTED'}
    hypotheses=bind.execute(sa.text('SELECT id,diagnosis_run_id,title,fault_domain,status,confidence,rationale,confirmable,confirm_rule,created_at FROM hypotheses')).mappings().all()
    for h in hypotheses:
        rid=str(uuid.uuid4()); state=status_map.get(h['status'],h['status'])
        bind.execute(sa.text('''INSERT INTO hypothesis_revisions
            (id,hypothesis_id,diagnosis_run_id,revision_no,supersedes_revision_id,title,fault_domain,status,confidence,rationale,confirmable,confirm_rule,created_at)
            VALUES (:id,:hid,:dr,1,NULL,:title,:fd,:status,:confidence,:rationale,:confirmable,:confirm_rule,:created_at)'''),
            {'id':rid,'hid':h['id'],'dr':h['diagnosis_run_id'],'title':h['title'],'fd':h['fault_domain'],'status':state,
             'confidence':h['confidence'],'rationale':h['rationale'],'confirmable':h['confirmable'],'confirm_rule':h['confirm_rule'],'created_at':h['created_at']})
        bind.execute(sa.text('UPDATE hypotheses SET status=:s,current_revision_id=:r WHERE id=:id'),{'s':state,'r':rid,'id':h['id']})
        bind.execute(sa.text('UPDATE hypothesis_evidence SET hypothesis_revision_id=:r WHERE hypothesis_id=:id AND hypothesis_revision_id IS NULL'),{'r':rid,'id':h['id']})

    # Job dependencies / idempotency / SSE outbox.
    op.create_table('job_dependencies',
        sa.Column('id',sa.String(36),primary_key=True),sa.Column('job_id',sa.String(36),sa.ForeignKey('jobs.id',ondelete='CASCADE'),nullable=False),
        sa.Column('depends_on_job_id',sa.String(36),sa.ForeignKey('jobs.id',ondelete='RESTRICT'),nullable=False),sa.Column('policy',sa.String(64),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),sa.UniqueConstraint('job_id','depends_on_job_id',name='uq_job_dependency'))
    op.create_index('ix_job_dependencies_job_id','job_dependencies',['job_id']); op.create_index('ix_job_dependencies_depends_on_job_id','job_dependencies',['depends_on_job_id'])

    op.create_table('idempotency_records',
        sa.Column('id',sa.String(36),primary_key=True),sa.Column('scope',sa.String(128),nullable=False),sa.Column('idempotency_key',sa.String(255),nullable=False),
        sa.Column('request_hash',sa.String(64),nullable=False),sa.Column('status',sa.String(32),nullable=False),sa.Column('response_status',sa.Integer(),nullable=True),
        sa.Column('response_json',sa.JSON(),nullable=True),sa.Column('resource_type',sa.String(64),nullable=True),sa.Column('resource_id',sa.String(128),nullable=True),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),sa.Column('updated_at',sa.DateTime(timezone=True),nullable=False),sa.Column('expires_at',sa.DateTime(timezone=True),nullable=True),
        sa.UniqueConstraint('scope','idempotency_key',name='uq_idempotency_scope_key'))
    for name,cols in [('ix_idempotency_records_scope',['scope']),('ix_idempotency_records_status',['status']),('ix_idempotency_records_resource_id',['resource_id']),('ix_idempotency_records_expires_at',['expires_at'])]: op.create_index(name,'idempotency_records',cols)

    op.create_table('event_outbox',
        sa.Column('seq',sa.Integer(),primary_key=True,autoincrement=True),sa.Column('event_id',sa.String(36),nullable=False,unique=True),
        sa.Column('event_type',sa.String(128),nullable=False),sa.Column('schema_version',sa.Integer(),nullable=False),sa.Column('case_id',sa.String(36),nullable=True),
        sa.Column('entity_type',sa.String(64),nullable=True),sa.Column('entity_id',sa.String(128),nullable=True),sa.Column('payload_json',sa.JSON(),nullable=True),sa.Column('created_at',sa.DateTime(timezone=True),nullable=False))
    for name,cols in [('ix_event_outbox_event_id',['event_id']),('ix_event_outbox_event_type',['event_type']),('ix_event_outbox_case_id',['case_id']),('ix_event_outbox_entity_id',['entity_id']),('ix_event_outbox_created_at',['created_at'])]: op.create_index(name,'event_outbox',cols)

    # RBAC persistence. Runtime supports header identity in dev/e2e; production can bind an external IdP subject to these tables.
    op.create_table('users',sa.Column('id',sa.String(36),primary_key=True),sa.Column('external_subject',sa.String(256),nullable=False,unique=True),sa.Column('display_name',sa.String(256)),sa.Column('active',sa.Boolean(),nullable=False),sa.Column('created_at',sa.DateTime(timezone=True),nullable=False))
    op.create_index('ix_users_external_subject','users',['external_subject'],unique=True)
    op.create_table('roles',sa.Column('id',sa.String(36),primary_key=True),sa.Column('name',sa.String(64),nullable=False,unique=True),sa.Column('created_at',sa.DateTime(timezone=True),nullable=False)); op.create_index('ix_roles_name','roles',['name'],unique=True)
    op.create_table('permissions',sa.Column('id',sa.String(36),primary_key=True),sa.Column('name',sa.String(128),nullable=False,unique=True),sa.Column('created_at',sa.DateTime(timezone=True),nullable=False)); op.create_index('ix_permissions_name','permissions',['name'],unique=True)
    op.create_table('user_roles',sa.Column('id',sa.String(36),primary_key=True),sa.Column('user_id',sa.String(36),sa.ForeignKey('users.id',ondelete='CASCADE'),nullable=False),sa.Column('role_id',sa.String(36),sa.ForeignKey('roles.id',ondelete='RESTRICT'),nullable=False),sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),sa.UniqueConstraint('user_id','role_id',name='uq_user_role'))
    op.create_index('ix_user_roles_user_id','user_roles',['user_id']); op.create_index('ix_user_roles_role_id','user_roles',['role_id'])
    op.create_table('role_permissions',sa.Column('id',sa.String(36),primary_key=True),sa.Column('role_id',sa.String(36),sa.ForeignKey('roles.id',ondelete='CASCADE'),nullable=False),sa.Column('permission_id',sa.String(36),sa.ForeignKey('permissions.id',ondelete='RESTRICT'),nullable=False),sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),sa.UniqueConstraint('role_id','permission_id',name='uq_role_permission'))
    op.create_index('ix_role_permissions_role_id','role_permissions',['role_id']); op.create_index('ix_role_permissions_permission_id','role_permissions',['permission_id'])

    # Contract status vocabulary migration. Keep only compatibility reads in code for old exported snapshots.
    for table in ['jobs','action_runs','analyzer_runs','rule_replay_runs']:
        bind.execute(sa.text(f"UPDATE {table} SET status='SUCCESS' WHERE status='SUCCEEDED'"))


def downgrade():
    # Downgrade is intentionally conservative: remove new structures/columns but keep business rows.
    for table in ['role_permissions','user_roles','permissions','roles','users','event_outbox','idempotency_records','job_dependencies']:
        op.drop_table(table)
    op.drop_constraint('fk_hypothesis_evidence_revision','hypothesis_evidence',type_='foreignkey')
    op.drop_index('ix_hypothesis_evidence_hypothesis_revision_id',table_name='hypothesis_evidence'); op.drop_column('hypothesis_evidence','hypothesis_revision_id')
    op.drop_table('hypothesis_revisions')
    op.drop_index('ix_hypotheses_current_revision_id',table_name='hypotheses'); op.drop_column('hypotheses','current_revision_id')
    for name in ['ix_analyzer_runs_scope','ix_analyzer_runs_config_checksum']: op.drop_index(name,table_name='analyzer_runs')
    for col in ['output_evidence_ids','scope','config_snapshot','config_checksum']: op.drop_column('analyzer_runs',col)
    op.drop_table('evidence_relations')
    for name in ['ix_evidences_call_id','ix_evidences_attempt_id','ix_evidences_session_id','ix_evidences_producer_id','ix_evidences_completeness','ix_evidences_level','ix_evidences_source_scope','ix_evidences_kind','ix_evidences_type']:
        op.drop_index(name,table_name='evidences')
    for col in ['call_id','attempt_id','session_id','producer_version','producer_id','producer_type','time_range_end','time_range_start','captured_at','completeness','level','source_scope','kind']:
        op.drop_column('evidences',col)
    op.drop_constraint('fk_evidences_case_restrict','evidences',type_='foreignkey')
    op.create_foreign_key('evidences_case_id_fkey','evidences','cases',['case_id'],['id'],ondelete='CASCADE')
