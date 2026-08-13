"""phase f2 production hardening feishu binding

Revision ID: 0011_phase_f2_production_hardening
Revises: 0010_phase_c3_diagnostic_experiments
"""
from alembic import op
import sqlalchemy as sa

revision='0011_phase_f2_production_hardening'
down_revision='0010_phase_c3_diagnostic_experiments'
branch_labels=None
depends_on=None


def upgrade():
    op.create_table(
        'feishu_case_bindings',
        sa.Column('id',sa.String(36),primary_key=True),
        sa.Column('case_id',sa.String(36),sa.ForeignKey('cases.id',ondelete='CASCADE'),nullable=False),
        sa.Column('receive_id',sa.String(256),nullable=False),
        sa.Column('receive_id_type',sa.String(32),nullable=False,server_default='chat_id'),
        sa.Column('message_id',sa.String(256),nullable=False),
        sa.Column('status',sa.String(32),nullable=False,server_default='ACTIVE'),
        sa.Column('card_version',sa.Integer(),nullable=False,server_default='1'),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),
        sa.Column('updated_at',sa.DateTime(timezone=True),nullable=False),
        sa.UniqueConstraint('case_id',name='uq_feishu_case_binding_case'),
        sa.UniqueConstraint('message_id',name='uq_feishu_case_binding_message'),
    )
    op.create_index('ix_feishu_case_bindings_case_id','feishu_case_bindings',['case_id'])
    op.create_index('ix_feishu_case_bindings_message_id','feishu_case_bindings',['message_id'])
    op.create_index('ix_feishu_case_bindings_status','feishu_case_bindings',['status'])


def downgrade():
    op.drop_table('feishu_case_bindings')
