"""device_credentials

Revision ID: 0012_device_credentials
Revises: 0011_phase_f2_production_hardening
Create Date: 2026-08-14

Adds the device_credentials table: DUT SSH host/port/user/password resolved from
Poseidon and provisioned for background reproduction (stored in DB, not secret.yaml).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0012_device_credentials'
down_revision: Union[str, None] = '0011_phase_f2_production_hardening'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'device_credentials',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('sn', sa.String(length=128), nullable=False),
        sa.Column('ip', sa.String(length=128), nullable=False),
        sa.Column('ssh_port', sa.Integer(), nullable=False),
        sa.Column('username', sa.String(length=64), nullable=False),
        sa.Column('password', sa.Text(), nullable=False),
        sa.Column('mac', sa.String(length=64), nullable=True),
        sa.Column('product', sa.String(length=128), nullable=True),
        sa.Column('web_url', sa.Text(), nullable=True),
        sa.Column('source', sa.String(length=32), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('sn', name='uq_device_credential_sn'),
    )
    op.create_index(op.f('ix_device_credentials_sn'), 'device_credentials', ['sn'], unique=True)


def downgrade() -> None:
    op.drop_index(op.f('ix_device_credentials_sn'), table_name='device_credentials')
    op.drop_table('device_credentials')
