"""Capture Engine V2.1 reliable segment ledger
Revision ID: 0028_capture_v2_reliable_segments
Revises: 0027_capture_v2_foundation
Create Date: 2026-08-20
"""
from alembic import op
import sqlalchemy as sa
revision="0028_capture_v2_reliable_segments"
down_revision="0027_capture_v2_foundation"
branch_labels=None
depends_on=None

def upgrade():
    op.create_table("capture_segments",
        sa.Column("id",sa.String(36),primary_key=True),
        sa.Column("capture_session_id",sa.String(36),sa.ForeignKey("capture_sessions.id",ondelete="CASCADE"),nullable=False),
        sa.Column("capture_epoch_id",sa.String(36),sa.ForeignKey("capture_epochs.id",ondelete="CASCADE"),nullable=False),
        sa.Column("device_id",sa.String(36),sa.ForeignKey("case_devices.id",ondelete="RESTRICT"),nullable=False),
        sa.Column("segment_seq",sa.BigInteger(),nullable=False),sa.Column("remote_path",sa.Text(),nullable=False),sa.Column("remote_inode",sa.BigInteger(),nullable=False),sa.Column("remote_size",sa.BigInteger(),nullable=False),sa.Column("remote_mtime_epoch",sa.BigInteger(),nullable=True),
        sa.Column("state",sa.String(32),nullable=False,server_default="DISCOVERED"),sa.Column("retention_state",sa.String(24),nullable=False,server_default="ROLLING"),sa.Column("transfer_attempts",sa.Integer(),nullable=False,server_default="0"),
        sa.Column("last_error_code",sa.String(128),nullable=True),sa.Column("last_error_detail",sa.JSON(),nullable=True),sa.Column("local_temp_path",sa.Text(),nullable=True),sa.Column("storage_key",sa.Text(),nullable=True),sa.Column("server_size",sa.BigInteger(),nullable=True),sa.Column("sha256",sa.String(64),nullable=True),
        sa.Column("pcap_valid",sa.Boolean(),nullable=True),sa.Column("packet_count",sa.BigInteger(),nullable=True),sa.Column("first_packet_ts",sa.DateTime(timezone=True),nullable=True),sa.Column("last_packet_ts",sa.DateTime(timezone=True),nullable=True),
        sa.Column("discovered_at",sa.DateTime(timezone=True),nullable=False,server_default=sa.func.now()),sa.Column("download_started_at",sa.DateTime(timezone=True),nullable=True),sa.Column("downloaded_at",sa.DateTime(timezone=True),nullable=True),sa.Column("verified_at",sa.DateTime(timezone=True),nullable=True),sa.Column("persisted_at",sa.DateTime(timezone=True),nullable=True),sa.Column("ack_pending_at",sa.DateTime(timezone=True),nullable=True),sa.Column("acked_at",sa.DateTime(timezone=True),nullable=True),sa.Column("remote_deleted_at",sa.DateTime(timezone=True),nullable=True),sa.Column("lease_epoch_at_ack",sa.BigInteger(),nullable=True),sa.Column("version",sa.Integer(),nullable=False,server_default="0"),
        sa.UniqueConstraint("capture_epoch_id","segment_seq",name="uq_capture_segment_epoch_seq"),sa.CheckConstraint("segment_seq > 0",name="ck_capture_segment_seq_positive"),sa.CheckConstraint("remote_inode > 0",name="ck_capture_segment_inode_positive"),sa.CheckConstraint("remote_size >= 24",name="ck_capture_segment_size_pcap_header"),
        sa.CheckConstraint("state IN ('DISCOVERED','TRANSFERRING','DOWNLOADED','VERIFIED','PERSISTING','PERSISTED','ACK_PENDING','ACKED','REMOTE_DELETED','ERROR')",name="ck_capture_segment_state"),sa.CheckConstraint("retention_state IN ('ROLLING','PINNED','RELEASED')",name="ck_capture_segment_retention"))
    op.create_index("ix_capture_segment_epoch_seq","capture_segments",["capture_epoch_id","segment_seq"]);op.create_index("ix_capture_segment_session_state","capture_segments",["capture_session_id","state"]);op.create_index("ix_capture_segment_device_state","capture_segments",["device_id","state"]);op.create_index("ix_capture_segment_sha256","capture_segments",["sha256"]);op.create_index("ix_capture_segment_storage_key","capture_segments",["storage_key"]);op.create_index("ix_capture_segment_error","capture_segments",["last_error_code"])
def downgrade():
    for n in ["ix_capture_segment_error","ix_capture_segment_storage_key","ix_capture_segment_sha256","ix_capture_segment_device_state","ix_capture_segment_session_state","ix_capture_segment_epoch_seq"]:op.drop_index(n,table_name="capture_segments")
    op.drop_table("capture_segments")
