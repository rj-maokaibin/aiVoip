from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.capture_v2.db_models import (
    CaptureEpoch, CaptureGap, CaptureSegment, CaptureSession, QualitySnapshot,
)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


@dataclass(frozen=True)
class CaptureTelemetrySnapshot:
    device_id: str | None
    producer_count_per_dut: int
    capture_gap_total: int
    unacked_segment_count: int
    unacked_bytes: int
    oldest_unacked_age: float
    segment_generation_rate: float
    segment_transfer_rate: float
    dut_spool_free_bytes: int | None
    sftp_failure_rate: float
    capture_complete_rate: float
    capture_partial_rate: float
    capture_failed_rate: float
    ready_prepare_latency: float | None
    alerts: tuple[str, ...]


class CaptureTelemetryCollector:
    """Deterministic P0 telemetry projection over the V2 ledger.

    `dut_spool_free_bytes` is an observed DUT value injected by the watchdog path;
    all other metrics are calculated from authoritative V2 DB records.
    """

    _UNACKED = ("DISCOVERED", "TRANSFERRING", "DOWNLOADED", "VERIFIED", "PERSISTING", "PERSISTED", "ACK_PENDING")

    def __init__(self, session_factory):
        self.session_factory = session_factory

    def collect(self, *, device_id: str | None = None, now: datetime | None = None,
                window_seconds: int = 60, dut_spool_free_bytes: int | None = None) -> CaptureTelemetrySnapshot:
        now = _utc(now or datetime.now(timezone.utc))
        since = now - timedelta(seconds=max(1, int(window_seconds)))
        with self.session_factory() as db:
            epoch_q = select(func.count(CaptureEpoch.id)).where(CaptureEpoch.state == "RUNNING")
            gap_q = select(func.count(CaptureGap.id))
            segment_q = select(CaptureSegment)
            session_q = select(CaptureSession)
            if device_id is not None:
                epoch_q = epoch_q.where(CaptureEpoch.device_id == device_id)
                gap_q = gap_q.join(CaptureSession, CaptureGap.capture_session_id == CaptureSession.id).where(CaptureSession.device_id == device_id)
                segment_q = segment_q.where(CaptureSegment.device_id == device_id)
                session_q = session_q.where(CaptureSession.device_id == device_id)
            producer_count = int(db.scalar(epoch_q) or 0)
            gaps = int(db.scalar(gap_q) or 0)
            segments = list(db.scalars(segment_q))
            sessions = list(db.scalars(session_q))
            session_ids = [s.id for s in sessions]
            qualities = list(db.scalars(select(QualitySnapshot).where(
                QualitySnapshot.capture_session_id.in_(session_ids)
            ))) if session_ids else []

        unacked = [s for s in segments if s.state in self._UNACKED]
        unacked_bytes = sum(int(s.remote_size or 0) for s in unacked)
        ages = [(now - _utc(s.discovered_at)).total_seconds() for s in unacked]
        generated = [s for s in segments if _utc(s.discovered_at) >= since]
        persisted = [s for s in segments if s.persisted_at is not None and _utc(s.persisted_at) >= since]
        failed_transfers = [s for s in generated if (s.last_error_code or "").startswith(("SFTP", "TRANSFER", "REMOTE_"))]
        q_total = len(qualities)
        def qrate(status: str) -> float:
            return (sum(1 for q in qualities if q.capture_completeness == status) / q_total) if q_total else 0.0
        ready_latencies = [
            (_utc(s.path_ready_at) - _utc(s.created_at)).total_seconds()
            for s in sessions if s.path_ready_at is not None
        ]
        alerts = ("P0_MULTIPLE_PRODUCERS_PER_DUT",) if producer_count > 1 else ()
        window = float(max(1, int(window_seconds)))
        return CaptureTelemetrySnapshot(
            device_id=device_id,
            producer_count_per_dut=producer_count,
            capture_gap_total=gaps,
            unacked_segment_count=len(unacked),
            unacked_bytes=unacked_bytes,
            oldest_unacked_age=max(ages, default=0.0),
            segment_generation_rate=len(generated) / window,
            segment_transfer_rate=sum(int(s.server_size or s.remote_size or 0) for s in persisted) / window,
            dut_spool_free_bytes=dut_spool_free_bytes,
            sftp_failure_rate=(len(failed_transfers) / len(generated)) if generated else 0.0,
            capture_complete_rate=qrate("COMPLETE"),
            capture_partial_rate=qrate("PARTIAL"),
            capture_failed_rate=qrate("FAILED"),
            ready_prepare_latency=(sum(ready_latencies) / len(ready_latencies)) if ready_latencies else None,
            alerts=alerts,
        )
