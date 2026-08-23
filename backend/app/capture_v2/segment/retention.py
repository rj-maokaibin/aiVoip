from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from app.capture_v2.db_models import CaptureEpoch, CaptureSegment, CoverageWindow
from app.capture_v2.errors import CaptureV2Error


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class SegmentRetentionService:
    """Server rolling-ring lifecycle without deleting durable evidence.

    Pinning is intentionally conservative: every Segment in a CaptureEpoch that
    overlaps the deterministic CoverageWindow is pinned. This safely retains
    header-only/silent Segments which have no packet timestamps and avoids using
    filename cadence as a time oracle.
    """

    def __init__(self, session_factory):
        self.session_factory = session_factory

    def pin_for_coverage_window(self, coverage_window_id: str) -> tuple[str, ...]:
        with self.session_factory() as db:
            with db.begin():
                window = db.get(CoverageWindow, coverage_window_id)
                if window is None:
                    raise CaptureV2Error("COVERAGE_WINDOW_NOT_FOUND")
                start, end = _utc(window.required_start_ts), _utc(window.required_end_ts)
                epochs = list(db.scalars(select(CaptureEpoch).where(
                    CaptureEpoch.capture_session_id == window.capture_session_id
                )))
                epoch_ids = []
                for epoch in epochs:
                    estart = _utc(epoch.started_at)
                    eend = _utc(epoch.ended_at) if epoch.ended_at is not None else end
                    if estart < end and eend > start:
                        epoch_ids.append(epoch.id)
                if not epoch_ids:
                    return ()
                rows = list(db.scalars(select(CaptureSegment).where(
                    CaptureSegment.capture_epoch_id.in_(epoch_ids)
                )))
                ids = []
                for row in rows:
                    if row.retention_state != "PINNED":
                        row.retention_state = "PINNED"
                    ids.append(row.id)
                return tuple(ids)

    def release_rolling_before(self, *, capture_session_id: str, cutoff: datetime) -> tuple[str, ...]:
        cutoff = _utc(cutoff)
        with self.session_factory() as db:
            with db.begin():
                rows = list(db.scalars(select(CaptureSegment).where(
                    CaptureSegment.capture_session_id == capture_session_id,
                    CaptureSegment.retention_state == "ROLLING",
                )))
                released = []
                for row in rows:
                    ts = row.persisted_at or row.discovered_at
                    if ts is not None and _utc(ts) < cutoff:
                        row.retention_state = "RELEASED"
                        released.append(row.id)
                return tuple(released)

    def release_pinned(self, *, capture_session_id: str) -> tuple[str, ...]:
        """Explicit retention-policy action; never called by capture finalization."""
        with self.session_factory() as db:
            with db.begin():
                rows = list(db.scalars(select(CaptureSegment).where(
                    CaptureSegment.capture_session_id == capture_session_id,
                    CaptureSegment.retention_state == "PINNED",
                )))
                for row in rows:
                    row.retention_state = "RELEASED"
                return tuple(row.id for row in rows)
