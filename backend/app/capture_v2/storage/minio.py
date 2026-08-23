from __future__ import annotations

from pathlib import Path

from app.capture_v2.errors import CaptureV2Error
from app.capture_v2.storage.base import DurableSegmentStore, PersistedObject


class MinioDurableSegmentStore(DurableSegmentStore):
    def __init__(self, client, bucket: str):
        self.client = client
        self.bucket = bucket

    def verify(self, *, storage_key: str, size: int, sha256: str) -> bool:
        try:
            stat = self.client.stat_object(self.bucket, storage_key)
        except Exception:
            return False
        meta = {str(k).lower(): str(v) for k, v in (getattr(stat, "metadata", {}) or {}).items()}
        stored_sha = meta.get("x-amz-meta-sha256") or meta.get("sha256")
        return int(getattr(stat, "size", -1)) == int(size) and stored_sha == sha256

    def persist(self, *, source_path: Path, storage_key: str, sha256: str) -> PersistedObject:
        source_path = Path(source_path)
        size = source_path.stat().st_size
        try:
            stat = self.client.stat_object(self.bucket, storage_key)
        except Exception:
            stat = None
        if stat is not None:
            if self.verify(storage_key=storage_key, size=size, sha256=sha256):
                return PersistedObject(storage_key, size, sha256)
            raise CaptureV2Error("SEGMENT_INTEGRITY_CONFLICT", details={"storage_key": storage_key})
        try:
            with source_path.open("rb") as fh:
                self.client.put_object(
                    self.bucket, storage_key, fh, length=size,
                    content_type="application/vnd.tcpdump.pcap",
                    metadata={"sha256": sha256},
                )
        except Exception as exc:
            raise CaptureV2Error("SERVER_STORE_PERSIST_FAILED", details={"exception": type(exc).__name__}) from exc
        if not self.verify(storage_key=storage_key, size=size, sha256=sha256):
            raise CaptureV2Error("SERVER_STORE_VERIFY_FAILED", details={"storage_key": storage_key})
        return PersistedObject(storage_key, size, sha256)
