from __future__ import annotations

from pathlib import Path

from app.capture_v2.errors import CaptureV2Error
from app.capture_v2.segment.repository import SegmentRepository, utcnow


class SegmentReconciler:
    def __init__(self, *, session_factory, store, persister):
        self.session_factory = session_factory
        self.store = store
        self.persister = persister

    async def reconcile_segment(self, segment_id: str) -> str:
        with self.session_factory() as db:
            row = SegmentRepository(db).by_id(segment_id)
            if row is None:
                raise CaptureV2Error("SEGMENT_NOT_FOUND")
            state = row.state
            key = row.storage_key
            size = row.server_size
            sha = row.sha256
            local = row.local_temp_path

        if key and size is not None and sha and self.store.verify(storage_key=key, size=size, sha256=sha):
            if state in ("PERSISTING", "ERROR", "VERIFIED", "DOWNLOADED"):
                with self.session_factory() as db:
                    with db.begin():
                        row = SegmentRepository(db).by_id(segment_id)
                        row.state = "PERSISTED"
                        row.persisted_at = row.persisted_at or utcnow()
                        row.last_error_code = None
                return "RECOVERED_PERSISTED"
            return state

        if local and Path(local).is_file() and state in ("DOWNLOADED", "VERIFIED", "PERSISTING", "ERROR"):
            self.persister.persist(segment_id, Path(local))
            return "RECOVERED_FROM_LOCAL_PART"

        if state in ("PERSISTED", "ACK_PENDING", "ACKED", "REMOTE_DELETED") and key:
            with self.session_factory() as db:
                with db.begin():
                    row = SegmentRepository(db).by_id(segment_id)
                    row.last_error_code = "SERVER_COPY_MISSING"
                    row.last_error_detail = {"storage_key": key}
                    if state not in ("ACKED", "REMOTE_DELETED"):
                        row.state = "DISCOVERED"
            return "SERVER_COPY_MISSING"
        return state
