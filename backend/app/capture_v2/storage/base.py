from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PersistedObject:
    storage_key: str
    size: int
    sha256: str


class DurableSegmentStore:
    def persist(self, *, source_path: Path, storage_key: str, sha256: str) -> PersistedObject:
        raise NotImplementedError

    def verify(self, *, storage_key: str, size: int, sha256: str) -> bool:
        raise NotImplementedError
