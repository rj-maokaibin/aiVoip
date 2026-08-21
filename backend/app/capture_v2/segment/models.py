from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RemoteSegmentIdentity:
    remote_path: str
    inode: int
    size: int
    mtime_epoch: int | None = None

    def as_dict(self) -> dict:
        return {
            "remote_path": self.remote_path,
            "inode": int(self.inode),
            "size": int(self.size),
            "mtime_epoch": self.mtime_epoch,
        }
