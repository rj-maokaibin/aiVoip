from __future__ import annotations

from pathlib import Path

from app.capture_v2.db_models import CaptureEvent
from app.capture_v2.errors import CaptureV2Error
from app.capture_v2.segment.pcap import validate_classic_pcap
from app.capture_v2.segment.repository import SegmentRepository, utcnow
from app.capture_v2.storage.local import sha256_file
from app.core.ids import new_id


class SegmentPersister:
    def __init__(self, session_factory, store):
        self.session_factory = session_factory
        self.store = store

    @staticmethod
    def storage_key(row) -> str:
        return f"capture-v2/{row.device_id}/{row.capture_epoch_id}/seg_{int(row.segment_seq):012d}.pcap"

    def persist(self, segment_id: str, local_path: Path) -> str:
        local_path = Path(local_path)
        validation = validate_classic_pcap(local_path)
        digest = sha256_file(local_path)
        with self.session_factory() as db:
            with db.begin():
                repo = SegmentRepository(db)
                row = repo.by_id(segment_id)
                if row is None:
                    raise CaptureV2Error("SEGMENT_NOT_FOUND")
                if local_path.stat().st_size != int(row.remote_size):
                    raise CaptureV2Error("SEGMENT_SIZE_MISMATCH")
                if row.sha256 is not None and row.sha256 != digest:
                    raise CaptureV2Error(
                        "SEGMENT_INTEGRITY_CONFLICT",
                        details={"reason": "COMMITTED_SHA256_MISMATCH", "segment_id": row.id},
                    )
                if row.state == "DOWNLOADED":
                    repo.transition(
                        row.id, expected="DOWNLOADED", next_state="VERIFIED",
                        verified_at=utcnow(), pcap_valid=True, packet_count=validation.packet_count,
                        first_packet_ts=validation.first_packet_ts, last_packet_ts=validation.last_packet_ts,
                        sha256=digest,
                    )
                row = repo.by_id(segment_id)
                if row.state == "VERIFIED":
                    repo.transition(row.id, expected="VERIFIED", next_state="PERSISTING")
                key = self.storage_key(row)
            persisted = self.store.persist(source_path=local_path, storage_key=key, sha256=digest)
            with db.begin():
                row = SegmentRepository(db).by_id(segment_id)
                if row.state not in ("PERSISTING", "PERSISTED"):
                    raise CaptureV2Error("SEGMENT_STATE_CONFLICT", details={"state": row.state})
                if row.state == "PERSISTING":
                    SegmentRepository(db).transition(
                        row.id, expected="PERSISTING", next_state="PERSISTED",
                        storage_key=persisted.storage_key, server_size=persisted.size,
                        sha256=persisted.sha256, persisted_at=utcnow(), local_temp_path=None,
                    )
                db.add(CaptureEvent(
                    id=new_id(), capture_session_id=row.capture_session_id,
                    entity_type="CAPTURE_SEGMENT", entity_id=row.id,
                    event_type="SEGMENT_PERSISTED", source_ts=utcnow(),
                    payload={"storage_key": key, "sha256": digest, "size": persisted.size},
                ))
            return key

    def repair_durable_copy(self, segment_id: str, local_path: Path) -> str:
        """Repair a missing Server copy without demoting an ACKED segment.

        ACKED is a one-way semantic boundary: the Server had durably persisted and
        committed this exact segment before ACK. If that durable object is later
        found missing while the DUT exact segment still exists, re-fetching and
        restoring the same deterministic object is a repair operation, not a state
        rollback. The bytes must match the committed size/SHA256 exactly.
        """
        local_path = Path(local_path)
        validation = validate_classic_pcap(local_path)
        digest = sha256_file(local_path)
        with self.session_factory() as db:
            row = SegmentRepository(db).by_id(segment_id)
            if row is None:
                raise CaptureV2Error("SEGMENT_NOT_FOUND")
            if row.state != "ACKED":
                raise CaptureV2Error("SEGMENT_STATE_CONFLICT", details={"state": row.state})
            if row.sha256 is None or row.server_size is None or row.storage_key is None:
                raise CaptureV2Error("ACKED_SEGMENT_DURABILITY_METADATA_MISSING")
            if local_path.stat().st_size != int(row.remote_size) or int(row.remote_size) != int(row.server_size):
                raise CaptureV2Error("SEGMENT_SIZE_MISMATCH")
            if digest != row.sha256:
                raise CaptureV2Error("SEGMENT_INTEGRITY_CONFLICT", details={"reason": "ACKED_SHA256_MISMATCH"})
            key = row.storage_key
            expected_sha = row.sha256
            expected_size = int(row.server_size)
        persisted = self.store.persist(source_path=local_path, storage_key=key, sha256=expected_sha)
        if int(persisted.size) != expected_size or persisted.sha256 != expected_sha:
            raise CaptureV2Error("SEGMENT_INTEGRITY_CONFLICT", details={"reason": "REPAIR_VERIFY_MISMATCH"})
        with self.session_factory() as db:
            with db.begin():
                row = SegmentRepository(db).by_id(segment_id)
                if row is None or row.state != "ACKED":
                    raise CaptureV2Error("SEGMENT_STATE_CONFLICT", details={"state": getattr(row, "state", None)})
                row.last_error_code = None
                row.last_error_detail = {}
                db.add(CaptureEvent(
                    id=new_id(), capture_session_id=row.capture_session_id,
                    entity_type="CAPTURE_SEGMENT", entity_id=row.id,
                    event_type="SEGMENT_SERVER_COPY_REPAIRED", source_ts=utcnow(),
                    payload={"storage_key": key, "sha256": expected_sha,
                             "size": expected_size, "packet_count": validation.packet_count},
                ))
        return key
