from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from app.capture_v2.db_models import CaptureSegment
from app.capture_v2.errors import CaptureV2Error
from app.core.ids import new_id


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


_ALLOWED = {
    "DISCOVERED": {"TRANSFERRING", "ERROR"},
    "TRANSFERRING": {"DOWNLOADED", "ERROR"},
    "DOWNLOADED": {"VERIFIED", "ERROR"},
    "VERIFIED": {"PERSISTING", "ERROR"},
    "PERSISTING": {"PERSISTED", "ERROR"},
    "PERSISTED": {"ACK_PENDING", "ERROR"},
    "ACK_PENDING": {"ACKED", "ERROR"},
    "ACKED": {"REMOTE_DELETED"},
    "ERROR": {"DISCOVERED", "TRANSFERRING", "PERSISTING", "ACK_PENDING", "ACKED"},
    "REMOTE_DELETED": set(),
}


class SegmentRepository:
    def __init__(self, db):
        self.db = db

    def by_id(self, segment_id: str) -> CaptureSegment | None:
        return self.db.get(CaptureSegment, segment_id)

    def by_identity(self, capture_epoch_id: str, segment_seq: int) -> CaptureSegment | None:
        return self.db.scalar(select(CaptureSegment).where(
            CaptureSegment.capture_epoch_id == capture_epoch_id,
            CaptureSegment.segment_seq == int(segment_seq),
        ))

    def discover(self, *, capture_session_id: str, capture_epoch_id: str, device_id: str,
                 segment_seq: int, remote_path: str, remote_inode: int, remote_size: int,
                 remote_mtime_epoch: int | None = None) -> CaptureSegment:
        existing = self.by_identity(capture_epoch_id, segment_seq)
        if existing is not None:
            if (existing.remote_path != remote_path or int(existing.remote_inode) != int(remote_inode)
                    or int(existing.remote_size) != int(remote_size)):
                raise CaptureV2Error("SEGMENT_IDENTITY_CONFLICT", details={
                    "segment_id": existing.id, "segment_seq": segment_seq,
                })
            return existing
        row = CaptureSegment(
            id=new_id(), capture_session_id=capture_session_id, capture_epoch_id=capture_epoch_id,
            device_id=device_id, segment_seq=int(segment_seq), remote_path=remote_path,
            remote_inode=int(remote_inode), remote_size=int(remote_size),
            remote_mtime_epoch=remote_mtime_epoch, state="DISCOVERED",
        )
        self.db.add(row)
        self.db.flush()
        return row

    def transition(self, segment_id: str, *, expected: str | tuple[str, ...], next_state: str, **values) -> CaptureSegment:
        row = self.by_id(segment_id)
        if row is None:
            raise CaptureV2Error("SEGMENT_NOT_FOUND", details={"segment_id": segment_id})
        expected_set = {expected} if isinstance(expected, str) else set(expected)
        if row.state not in expected_set:
            raise CaptureV2Error("SEGMENT_STATE_CONFLICT", details={
                "segment_id": segment_id, "actual": row.state, "expected": sorted(expected_set),
            })
        if next_state not in _ALLOWED.get(row.state, set()):
            raise CaptureV2Error("SEGMENT_TRANSITION_INVALID", details={"from": row.state, "to": next_state})
        row.state = next_state
        row.version = int(row.version or 0) + 1
        for key, value in values.items():
            setattr(row, key, value)
        self.db.flush()
        return row

    def set_error(self, segment_id: str, code: str, detail: dict | None = None) -> CaptureSegment:
        row = self.by_id(segment_id)
        if row is None:
            raise CaptureV2Error("SEGMENT_NOT_FOUND")
        # ACKED is a one-way Server-durability boundary. GC/fencing failures may
        # update error metadata but must never demote durable evidence to ERROR.
        if row.state not in ("ACKED", "REMOTE_DELETED"):
            row.state = "ERROR"
        row.last_error_code = code
        row.last_error_detail = detail or {}
        row.version = int(row.version or 0) + 1
        self.db.flush()
        return row

    def unacked(self, capture_session_id: str) -> list[CaptureSegment]:
        return list(self.db.scalars(select(CaptureSegment).where(
            CaptureSegment.capture_session_id == capture_session_id,
            CaptureSegment.state.not_in(("ACKED", "REMOTE_DELETED")),
        ).order_by(CaptureSegment.discovered_at, CaptureSegment.segment_seq)))
