from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from pathlib import Path

from app.capture_v2.errors import CaptureV2Error
from app.capture_v2.storage.base import DurableSegmentStore, PersistedObject


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


class LocalDurableSegmentStore(DurableSegmentStore):
    """POSIX durable store that never overwrites an existing evidence object."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        return self.root / key

    def verify(self, *, storage_key: str, size: int, sha256: str) -> bool:
        path = self._path(storage_key)
        return path.is_file() and path.stat().st_size == int(size) and sha256_file(path) == sha256

    def persist(self, *, source_path: Path, storage_key: str, sha256: str) -> PersistedObject:
        source_path = Path(source_path)
        size = source_path.stat().st_size
        if sha256_file(source_path) != sha256:
            raise CaptureV2Error("SERVER_SOURCE_SHA256_MISMATCH")
        dst = self._path(storage_key)
        dst.parent.mkdir(parents=True, exist_ok=True)

        if dst.exists():
            if self.verify(storage_key=storage_key, size=size, sha256=sha256):
                return PersistedObject(storage_key, size, sha256)
            raise CaptureV2Error("SEGMENT_INTEGRITY_CONFLICT", details={"storage_key": storage_key})

        fd, tmp_name = tempfile.mkstemp(prefix=f".{dst.name}.part.", dir=str(dst.parent))
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as out, source_path.open("rb") as src:
                shutil.copyfileobj(src, out, 1024 * 1024)
                out.flush()
                os.fsync(out.fileno())

            # link() is atomic create-if-absent on the same filesystem. Unlike
            # os.replace(), it can never overwrite an already committed evidence
            # object. A concurrent winner is accepted only when bytes match.
            try:
                os.link(tmp, dst)
            except FileExistsError:
                if not self.verify(storage_key=storage_key, size=size, sha256=sha256):
                    raise CaptureV2Error(
                        "SEGMENT_INTEGRITY_CONFLICT", details={"storage_key": storage_key}
                    )
            dfd = os.open(str(dst.parent), os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
            if not self.verify(storage_key=storage_key, size=size, sha256=sha256):
                raise CaptureV2Error("SERVER_STORE_VERIFY_FAILED", details={"storage_key": storage_key})
            return PersistedObject(storage_key, size, sha256)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
