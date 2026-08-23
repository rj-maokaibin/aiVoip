from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.capture_v2.enums import CaptureEpochState, CaptureHealth, CaptureLeaseState, CaptureSessionState
from app.core.ids import new_id
from app.db.base import Base

# V2 tables declare string FKs to app-owned tables (case_devices,
# reproduction_sessions) that live in app.db.models. Register those tables in
# the shared metadata before SQLAlchemy configures the V2 mappers, so any entry
# point that imports only the V2 models (e.g. the Gate CLI) can resolve the FK
# targets. app.db.models does not import capture_v2, so there is no cycle.
import app.db.models  # noqa: E402,F401


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class CaptureSession(Base):
    __tablename__ = "capture_sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    reproduction_session_id: Mapped[str] = mapped_column(
        ForeignKey("reproduction_sessions.id", ondelete="CASCADE"), unique=True, index=True
    )
    device_id: Mapped[str] = mapped_column(ForeignKey("case_devices.id", ondelete="RESTRICT"), index=True)
    state: Mapped[str] = mapped_column(String(40), index=True, default=CaptureSessionState.CREATED.value)
    health_status: Mapped[str] = mapped_column(String(24), index=True, default=CaptureHealth.HEALTHY.value)
    capture_profile_id: Mapped[str] = mapped_column(String(128))
    capture_profile_version: Mapped[str] = mapped_column(String(64))
    platform_profile_id: Mapped[str] = mapped_column(String(64))
    platform_profile_version: Mapped[str] = mapped_column(String(64))
    effective_profile: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    path_ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    target_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    evidence_durable_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    cleanup_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    schema_version: Mapped[str] = mapped_column(String(64), default="capture-v2.1.1")


class CaptureLease(Base):
    __tablename__ = "capture_leases"
    device_id: Mapped[str] = mapped_column(ForeignKey("case_devices.id", ondelete="CASCADE"), primary_key=True)
    capture_session_id: Mapped[str | None] = mapped_column(
        ForeignKey("capture_sessions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    owner_worker_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    state: Mapped[str] = mapped_column(String(24), index=True, default=CaptureLeaseState.RELEASED.value)
    acquired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    renewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class CaptureEpoch(Base):
    __tablename__ = "capture_epochs"
    __table_args__ = (
        UniqueConstraint("device_id", "epoch_token", name="uq_capture_epoch_device_token"),
        UniqueConstraint("capture_session_id", "epoch_index", name="uq_capture_epoch_session_index"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    capture_session_id: Mapped[str] = mapped_column(ForeignKey("capture_sessions.id", ondelete="CASCADE"), index=True)
    device_id: Mapped[str] = mapped_column(ForeignKey("case_devices.id", ondelete="RESTRICT"), index=True)
    epoch_index: Mapped[int] = mapped_column(BigInteger, nullable=False)
    epoch_token: Mapped[str] = mapped_column(String(128), nullable=False)
    boot_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    producer_pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    producer_starttime: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    producer_cmdline: Mapped[str | None] = mapped_column(Text, nullable=True)
    interface: Mapped[str | None] = mapped_column(String(128), nullable=True)
    capture_mode: Mapped[str] = mapped_column(String(32), default="FULL_VOICE")
    lease_epoch_started: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[str] = mapped_column(String(24), index=True, default=CaptureEpochState.STARTING.value)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    end_reason: Mapped[str | None] = mapped_column(String(128), nullable=True)
    packets_captured: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    packets_received: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    packets_dropped_kernel: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class CaptureEvent(Base):
    __tablename__ = "capture_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    capture_session_id: Mapped[str] = mapped_column(ForeignKey("capture_sessions.id", ondelete="CASCADE"), index=True)
    entity_type: Mapped[str] = mapped_column(String(64), index=True)
    entity_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    source_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    schema_version: Mapped[str] = mapped_column(String(64), default="capture-event-v2.1")


class CaptureGap(Base):
    __tablename__ = "capture_gaps"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    capture_session_id: Mapped[str] = mapped_column(ForeignKey("capture_sessions.id", ondelete="CASCADE"), index=True)
    capture_epoch_id: Mapped[str | None] = mapped_column(
        ForeignKey("capture_epochs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    channel: Mapped[str] = mapped_column(String(32), index=True)
    gap_start_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    gap_end_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    certainty: Mapped[str] = mapped_column(String(16), index=True)
    reason_code: Mapped[str] = mapped_column(String(128), index=True)
    source: Mapped[str] = mapped_column(String(64), index=True)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    recovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    details: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class CaptureSegment(Base):
    __tablename__ = "capture_segments"
    __table_args__ = (
        UniqueConstraint("capture_epoch_id", "segment_seq", name="uq_capture_segment_epoch_seq"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    capture_session_id: Mapped[str] = mapped_column(ForeignKey("capture_sessions.id", ondelete="CASCADE"), index=True)
    capture_epoch_id: Mapped[str] = mapped_column(ForeignKey("capture_epochs.id", ondelete="CASCADE"), index=True)
    device_id: Mapped[str] = mapped_column(ForeignKey("case_devices.id", ondelete="RESTRICT"), index=True)
    segment_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    remote_path: Mapped[str] = mapped_column(Text, nullable=False)
    remote_inode: Mapped[int] = mapped_column(BigInteger, nullable=False)
    remote_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    remote_mtime_epoch: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="DISCOVERED", index=True)
    retention_state: Mapped[str] = mapped_column(String(24), nullable=False, default="ROLLING")
    transfer_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error_code: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    last_error_detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    local_temp_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    storage_key: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    server_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    pcap_valid: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    packet_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    first_packet_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_packet_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    download_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    downloaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    persisted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ack_pending_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    acked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    remote_deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_epoch_at_ack: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class ReadinessSnapshot(Base):
    __tablename__ = "readiness_snapshots"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    capture_session_id: Mapped[str] = mapped_column(ForeignKey("capture_sessions.id", ondelete="CASCADE"), index=True)
    stage: Mapped[str] = mapped_column(String(40), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    checks: Mapped[dict] = mapped_column(JSON, default=dict)
    reasons: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    schema_version: Mapped[str] = mapped_column(String(64), default="readiness-v2.1")


class CaptureAttempt(Base):
    __tablename__ = "capture_attempts"
    __table_args__ = (UniqueConstraint("capture_session_id", "attempt_no", name="uq_capture_attempt_session_no"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    capture_session_id: Mapped[str] = mapped_column(ForeignKey("capture_sessions.id", ondelete="CASCADE"), index=True)
    reproduction_attempt_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(32), index=True)
    candidate_start_source_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confirmed_start_source_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_source_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confirmation_source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    classification: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class AttemptDataPlaneVerification(Base):
    __tablename__ = "attempt_data_plane_verifications"
    __table_args__ = (UniqueConstraint("capture_attempt_id", "channel", name="uq_attempt_data_plane_channel"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    capture_attempt_id: Mapped[str] = mapped_column(ForeignKey("capture_attempts.id", ondelete="CASCADE"), index=True)
    channel: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True, default="PENDING")
    expectation_created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    verification_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    first_seen_source_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reason_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class CoverageWindow(Base):
    __tablename__ = "coverage_windows"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False, unique=True, index=True)
    capture_session_id: Mapped[str] = mapped_column(ForeignKey("capture_sessions.id", ondelete="CASCADE"), index=True)
    capture_attempt_id: Mapped[str | None] = mapped_column(ForeignKey("capture_attempts.id", ondelete="SET NULL"), nullable=True, index=True)
    call_ref: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    window_type: Mapped[str] = mapped_column(String(40), index=True)
    required_start_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    required_end_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(24), index=True, default="PENDING")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    details: Mapped[dict] = mapped_column(JSON, default=dict)


class CoverageTrack(Base):
    __tablename__ = "coverage_tracks"
    __table_args__ = (UniqueConstraint("coverage_window_id", "channel", name="uq_coverage_window_channel"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    coverage_window_id: Mapped[str] = mapped_column(ForeignKey("coverage_windows.id", ondelete="CASCADE"), index=True)
    channel: Mapped[str] = mapped_column(String(32), index=True)
    requirement: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(24), index=True, default="PENDING")
    required_ms: Mapped[int] = mapped_column(BigInteger, default=0)
    covered_ms: Mapped[int] = mapped_column(BigInteger, default=0)
    gap_ms: Mapped[int] = mapped_column(BigInteger, default=0)
    unknown_ms: Mapped[int] = mapped_column(BigInteger, default=0)
    details: Mapped[dict] = mapped_column(JSON, default=dict)


class CoverageInterval(Base):
    __tablename__ = "coverage_intervals"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    coverage_track_id: Mapped[str] = mapped_column(ForeignKey("coverage_tracks.id", ondelete="CASCADE"), index=True)
    interval_start_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    interval_end_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    interval_type: Mapped[str] = mapped_column(String(24), index=True)
    source_kind: Mapped[str] = mapped_column(String(32), index=True)
    source_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    certainty: Mapped[str] = mapped_column(String(16), default="CONFIRMED")
    details: Mapped[dict] = mapped_column(JSON, default=dict)


class QualitySnapshot(Base):
    __tablename__ = "quality_snapshots"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False, unique=True, index=True)
    coverage_window_id: Mapped[str | None] = mapped_column(
        ForeignKey("coverage_windows.id", ondelete="SET NULL"), nullable=True, index=True
    )
    capture_session_id: Mapped[str] = mapped_column(ForeignKey("capture_sessions.id", ondelete="CASCADE"), index=True)
    capture_attempt_id: Mapped[str | None] = mapped_column(ForeignKey("capture_attempts.id", ondelete="SET NULL"), nullable=True, index=True)
    call_ref: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    capture_completeness: Mapped[str] = mapped_column(String(24), index=True)
    diagnostic_confidence: Mapped[str] = mapped_column(String(24), index=True)
    policy_version: Mapped[str] = mapped_column(String(64))
    reasons: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class SignalAvailability(Base):
    __tablename__ = "signal_availability"
    __table_args__ = (UniqueConstraint("quality_snapshot_id", "channel", name="uq_quality_signal_channel"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    quality_snapshot_id: Mapped[str] = mapped_column(ForeignKey("quality_snapshots.id", ondelete="CASCADE"), index=True)
    channel: Mapped[str] = mapped_column(String(32), index=True)
    availability: Mapped[str] = mapped_column(String(40), index=True)
    reason_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    details: Mapped[dict] = mapped_column(JSON, default=dict)


class EvidenceAsset(Base):
    __tablename__ = "evidence_assets"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    idempotency_key: Mapped[str | None] = mapped_column(String(160), nullable=True, unique=True, index=True)
    capture_session_id: Mapped[str] = mapped_column(ForeignKey("capture_sessions.id", ondelete="CASCADE"), index=True)
    capture_attempt_id: Mapped[str | None] = mapped_column(ForeignKey("capture_attempts.id", ondelete="SET NULL"), nullable=True, index=True)
    call_ref: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    asset_type: Mapped[str] = mapped_column(String(40), index=True)
    title: Mapped[str] = mapped_column(String(256))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    storage_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_refs: Mapped[list] = mapped_column(JSON, default=list)
    start_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    end_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
