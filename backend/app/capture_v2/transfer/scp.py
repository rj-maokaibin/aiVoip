from __future__ import annotations

from pathlib import Path

from app.capture_v2.errors import CaptureV2Error


class ExactScpDownloader:
    """Download one exact immutable remote file over SCP (dropbear).

    Platforms ship Dropbear without an SFTP subsystem (no sftp-server binary);
    SCP is the supported transfer protocol there. The downloader keeps the same
    exact-transfer contract as ExactSftpDownloader: one call performs one transfer
    only, and Capture V2 owns retry semantics above this layer so retries always
    target the same Segment identity.

    Gate-only selection: production composition keeps SFTP as the default; the
    real-gate tooling selects SCP explicitly via --transport scp on platforms that
    lack an SFTP subsystem (R3 BLOCKED_TRANSPORT unblock path).
    """

    def __init__(self, adapter):
        self.adapter = adapter

    async def get(self, *, remote_path: str, local_path: Path, timeout: float | None = None) -> None:
        local_path = Path(local_path)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        if local_path.exists():
            local_path.unlink()
        try:
            await self.adapter.scp_get(remote_path, str(local_path), timeout=timeout)
        except AttributeError as exc:
            raise CaptureV2Error("SCP_ADAPTER_NOT_INSTALLED") from exc
        except Exception as exc:
            raise CaptureV2Error("SCP_GET_FAILED", details={"exception": type(exc).__name__}) from exc
