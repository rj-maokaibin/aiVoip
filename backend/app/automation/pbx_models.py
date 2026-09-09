from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.ids import new_id
from app.db.base import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PbxNode(Base):
    __tablename__ = "pbx_nodes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    node_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    provider: Mapped[str] = mapped_column(String(32), index=True, default="fusionpbx")
    runtime_provider: Mapped[str] = mapped_column(String(32), default="freeswitch")
    host: Mapped[str] = mapped_column(String(255))
    fusionpbx_root: Mapped[str] = mapped_column(String(512))
    internal_profile: Mapped[str] = mapped_column(String(64), default="internal")
    internal_port: Mapped[int] = mapped_column(Integer, default=5060)
    status: Mapped[str] = mapped_column(String(32), index=True, default="UNKNOWN")
    source_fence_version: Mapped[str] = mapped_column(String(128))
    provider_metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    last_health_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class PbxExtensionResource(Base):
    __tablename__ = "pbx_extension_resources"
    __table_args__ = (
        UniqueConstraint("pbx_node_id", "domain_id", "extension", name="uq_pbx_extension_identity"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    pbx_node_id: Mapped[str] = mapped_column(ForeignKey("pbx_nodes.id", ondelete="RESTRICT"), index=True)
    domain_id: Mapped[str] = mapped_column(String(128), index=True)
    extension: Mapped[str] = mapped_column(String(128), index=True)
    dial_alias: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    dial_alias_resource_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    dial_alias_lease_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    dial_alias_lease_epoch: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    resource_type: Mapped[str] = mapped_column(String(32), index=True)
    state: Mapped[str] = mapped_column(String(32), index=True, default="FREE")
    lease_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    lease_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    owner_run_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    created_by_automation: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    deletable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ownership_marker: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_health_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_cleanup_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dirty_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class PbxResourceLease(Base):
    __tablename__ = "pbx_resource_leases"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    pbx_node_id: Mapped[str] = mapped_column(ForeignKey("pbx_nodes.id", ondelete="RESTRICT"), index=True)
    resource_id: Mapped[str] = mapped_column(ForeignKey("pbx_extension_resources.id", ondelete="RESTRICT"), index=True)
    run_id: Mapped[str] = mapped_column(String(128), index=True)
    owner_worker_id: Mapped[str] = mapped_column(String(128), index=True)
    lease_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[str] = mapped_column(String(24), index=True, default="ACTIVE")
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    renewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PbxMutationSnapshot(Base):
    __tablename__ = "pbx_mutation_snapshots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    resource_id: Mapped[str] = mapped_column(ForeignKey("pbx_extension_resources.id", ondelete="RESTRICT"), index=True)
    lease_id: Mapped[str] = mapped_column(ForeignKey("pbx_resource_leases.id", ondelete="RESTRICT"), index=True)
    run_id: Mapped[str] = mapped_column(String(128), index=True)
    mutation_kind: Mapped[str] = mapped_column(String(64), index=True)
    snapshot_json: Mapped[dict] = mapped_column(JSON)
    schema_version: Mapped[str] = mapped_column(String(64), default="pbx-mutation-snapshot-v1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PbxCallSession(Base):
    __tablename__ = "pbx_call_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(String(128), index=True)
    pbx_node_id: Mapped[str] = mapped_column(ForeignKey("pbx_nodes.id", ondelete="RESTRICT"), index=True)
    freeswitch_uuid: Mapped[str | None] = mapped_column(String(128), nullable=True, unique=True)
    caller_resource_id: Mapped[str | None] = mapped_column(ForeignKey("pbx_extension_resources.id", ondelete="RESTRICT"), nullable=True, index=True)
    callee_resource_id: Mapped[str | None] = mapped_column(ForeignKey("pbx_extension_resources.id", ondelete="RESTRICT"), nullable=True, index=True)
    direction: Mapped[str] = mapped_column(String(32))
    state: Mapped[str] = mapped_column(String(32), index=True, default="CREATED")
    ringing_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    hangup_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    hangup_cause: Mapped[str | None] = mapped_column(String(128), nullable=True)
    sip_evidence_refs_json: Mapped[list] = mapped_column(JSON, default=list)
    rtp_evidence_refs_json: Mapped[list] = mapped_column(JSON, default=list)
    dtmf_evidence_refs_json: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
