"""rules knowledge reports

Revision ID: 0005
Revises: 0004
"""
from alembic import op
import sqlalchemy as sa

revision='0005'; down_revision='0004'; branch_labels=None; depends_on=None

def upgrade():
    op.create_table('rule_definitions',
        sa.Column('id',sa.String(36),primary_key=True), sa.Column('rule_key',sa.String(128),nullable=False),
        sa.Column('name',sa.String(256),nullable=False), sa.Column('fault_domain',sa.String(128),nullable=False),
        sa.Column('enabled',sa.Integer(),nullable=False), sa.Column('active_version',sa.String(64),nullable=True),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False), sa.Column('updated_at',sa.DateTime(timezone=True),nullable=False),
        sa.UniqueConstraint('rule_key'))
    op.create_index('ix_rule_definitions_rule_key','rule_definitions',['rule_key'],unique=True)
    op.create_index('ix_rule_definitions_fault_domain','rule_definitions',['fault_domain'])
    op.create_table('rule_versions',
        sa.Column('id',sa.String(36),primary_key=True), sa.Column('rule_definition_id',sa.String(36),sa.ForeignKey('rule_definitions.id',ondelete='CASCADE'),nullable=False),
        sa.Column('version',sa.String(64),nullable=False), sa.Column('checksum',sa.String(64),nullable=False), sa.Column('status',sa.String(32),nullable=False),
        sa.Column('content_json',sa.JSON(),nullable=False), sa.Column('created_by',sa.String(128)), sa.Column('approved_by',sa.String(128)),
        sa.Column('change_note',sa.Text()), sa.Column('created_at',sa.DateTime(timezone=True),nullable=False), sa.Column('approved_at',sa.DateTime(timezone=True)),
        sa.UniqueConstraint('rule_definition_id','version',name='uq_rule_version'))
    op.create_index('ix_rule_versions_rule_definition_id','rule_versions',['rule_definition_id']); op.create_index('ix_rule_versions_checksum','rule_versions',['checksum']); op.create_index('ix_rule_versions_status','rule_versions',['status'])
    op.create_table('rule_replay_runs',
        sa.Column('id',sa.String(36),primary_key=True), sa.Column('case_id',sa.String(36),sa.ForeignKey('cases.id',ondelete='CASCADE'),nullable=False),
        sa.Column('rule_version_id',sa.String(36),sa.ForeignKey('rule_versions.id',ondelete='CASCADE'),nullable=False), sa.Column('status',sa.String(32),nullable=False),
        sa.Column('input_fingerprint',sa.String(64)), sa.Column('matched',sa.Integer(),nullable=False), sa.Column('result_json',sa.JSON()), sa.Column('created_by',sa.String(128)),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False), sa.Column('finished_at',sa.DateTime(timezone=True)))
    op.create_index('ix_rule_replay_runs_case_id','rule_replay_runs',['case_id']); op.create_index('ix_rule_replay_runs_rule_version_id','rule_replay_runs',['rule_version_id']); op.create_index('ix_rule_replay_runs_status','rule_replay_runs',['status'])
    op.create_table('knowledge_items',
        sa.Column('id',sa.String(36),primary_key=True), sa.Column('type',sa.String(64),nullable=False), sa.Column('title',sa.String(512),nullable=False), sa.Column('summary',sa.Text(),nullable=False),
        sa.Column('content_json',sa.JSON()), sa.Column('tags_json',sa.JSON()), sa.Column('source_ref',sa.String(1024)), sa.Column('status',sa.String(32),nullable=False),
        sa.Column('verified',sa.Integer(),nullable=False), sa.Column('verified_by',sa.String(128)), sa.Column('verified_at',sa.DateTime(timezone=True)), sa.Column('created_by',sa.String(128)), sa.Column('created_at',sa.DateTime(timezone=True),nullable=False), sa.Column('updated_at',sa.DateTime(timezone=True),nullable=False))
    op.create_index('ix_knowledge_items_type','knowledge_items',['type']); op.create_index('ix_knowledge_items_status','knowledge_items',['status'])
    op.create_table('case_relations',
        sa.Column('id',sa.String(36),primary_key=True), sa.Column('case_id',sa.String(36),sa.ForeignKey('cases.id',ondelete='CASCADE'),nullable=False),
        sa.Column('related_case_id',sa.String(36),sa.ForeignKey('cases.id',ondelete='CASCADE'),nullable=False), sa.Column('relation_type',sa.String(64),nullable=False), sa.Column('score',sa.Integer(),nullable=False),
        sa.Column('details_json',sa.JSON()), sa.Column('created_at',sa.DateTime(timezone=True),nullable=False), sa.Column('updated_at',sa.DateTime(timezone=True),nullable=False),
        sa.UniqueConstraint('case_id','related_case_id','relation_type',name='uq_case_relation'))
    op.create_index('ix_case_relations_case_id','case_relations',['case_id']); op.create_index('ix_case_relations_related_case_id','case_relations',['related_case_id']); op.create_index('ix_case_relations_relation_type','case_relations',['relation_type'])
    op.create_table('diagnosis_reports',
        sa.Column('id',sa.String(36),primary_key=True), sa.Column('case_id',sa.String(36),sa.ForeignKey('cases.id',ondelete='CASCADE'),nullable=False),
        sa.Column('diagnosis_run_id',sa.String(36),sa.ForeignKey('diagnosis_runs.id',ondelete='SET NULL')), sa.Column('version',sa.String(64),nullable=False), sa.Column('status',sa.String(32),nullable=False),
        sa.Column('html_object_key',sa.String(1024),nullable=False), sa.Column('json_object_key',sa.String(1024),nullable=False), sa.Column('snapshot_json',sa.JSON()), sa.Column('created_by',sa.String(128)), sa.Column('created_at',sa.DateTime(timezone=True),nullable=False))
    op.create_index('ix_diagnosis_reports_case_id','diagnosis_reports',['case_id']); op.create_index('ix_diagnosis_reports_diagnosis_run_id','diagnosis_reports',['diagnosis_run_id']); op.create_index('ix_diagnosis_reports_status','diagnosis_reports',['status'])

def downgrade():
    for table in ['diagnosis_reports','case_relations','knowledge_items','rule_replay_runs','rule_versions','rule_definitions']:
        op.drop_table(table)
