from __future__ import annotations

from pathlib import Path

from app.capture_v2.errors import CaptureV2Error


class ExactSftpDownloader:
    def __init__(self, adapter):
        self.adapter = adapter

    async def get(self, *, remote_path: str, local_path: Path, timeout: float | None = None) -> None:
        local_path = Path(local_path)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        if local_path.exists():
            local_path.unlink()
        try:
            await self.adapter.sftp_get(remote_path, str(local_path), timeout=timeout)
        except AttributeError as exc:
            raise CaptureV2Error("SFTP_ADAPTER_NOT_INSTALLED") from exc
        except Exception as exc:
            raise CaptureV2Error("SFTP_GET_FAILED", details={"exception": type(exc).__name__}) from exc
