from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path

from app.capture_v2.errors import CaptureV2Error
from app.capture_v2.gate.models import GateDeviceSpec


@dataclass(frozen=True)
class GateContext:
    device: GateDeviceSpec
    reproduction_session_id: str
    worker_id: str
    profile_root: Path
    requested_profile_id: str = "voip-standard"
    output_root: Path = Path("/tmp/capture-v2-gates")


def password_from_env(env_name: str = "CAPTURE_GATE_SSH_PASSWORD") -> str:
    value = os.getenv(env_name, "")
    if not value:
        raise CaptureV2Error(
            "CAPTURE_GATE_SSH_PASSWORD_MISSING",
            details={"env": env_name},
        )
    return value


class GateSftpAdapter:
    """Gate compatibility proxy for master revisions without ``sftp_get`` yet.

    The product integration patch still adds ``sftp_get`` to AsyncSSHDeviceAdapter.
    This proxy makes the validation branch runnable before that production patch is
    merged; it does not change retry semantics and one call is one exact transfer.
    """

    def __init__(self, adapter):
        self._adapter = adapter

    def __getattr__(self, name: str):
        return getattr(self._adapter, name)

    async def sftp_get(self, remote_path: str, local_path: str, timeout: float | None = None) -> None:
        native = getattr(type(self._adapter), "sftp_get", None)
        if native is not None:
            return await native(self._adapter, remote_path, local_path, timeout=timeout)
        conn = getattr(self._adapter, "conn", None)
        if conn is None:
            raise CaptureV2Error("SSH_NOT_CONNECTED")
        try:
            async def _get():
                async with conn.start_sftp_client() as sftp:
                    await sftp.get(remote_path, local_path, preserve=False)
            await asyncio.wait_for(_get(), timeout=timeout or 60.0)
        except asyncio.TimeoutError as exc:
            raise CaptureV2Error("SFTP_TIMEOUT") from exc
        except Exception as exc:
            raise CaptureV2Error("SFTP_GET_FAILED", details={"exception": type(exc).__name__}) from exc

    async def scp_get(self, remote_path: str, local_path: str, timeout: float | None = None) -> None:
        native = getattr(type(self._adapter), "scp_get", None)
        if native is not None:
            return await native(self._adapter, remote_path, local_path, timeout=timeout)
        conn = getattr(self._adapter, "conn", None)
        if conn is None:
            raise CaptureV2Error("SSH_NOT_CONNECTED")
        try:
            import asyncssh

            async def _get():
                await asyncssh.scp((conn, remote_path), local_path)
            await asyncio.wait_for(_get(), timeout=timeout or 60.0)
        except asyncio.TimeoutError as exc:
            raise CaptureV2Error("SCP_TIMEOUT") from exc
        except Exception as exc:
            raise CaptureV2Error("SCP_GET_FAILED", details={"exception": type(exc).__name__}) from exc


def build_asyncssh_adapter(spec: GateDeviceSpec, *, password_env: str = "CAPTURE_GATE_SSH_PASSWORD"):
    # Delayed import keeps unit tests independent from asyncssh.
    from app.collectors.asyncssh_adapter import AsyncSSHDeviceAdapter

    adapter = AsyncSSHDeviceAdapter(
        ip=spec.host,
        port=int(spec.port),
        username=spec.username,
        password=password_from_env(password_env),
    )
    return GateSftpAdapter(adapter)
