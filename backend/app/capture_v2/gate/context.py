from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select

from app.capture_v2.errors import CaptureV2Error
from app.capture_v2.gate.models import GateDeviceSpec
from app.db.models import DeviceCredential
from app.db.session import SessionLocal


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


def password_from_source(source: str = "CAPTURE_GATE_SSH_PASSWORD") -> str:
    """Resolve legacy Gate SSH password sources without logging plaintext."""
    ref = str(source or "CAPTURE_GATE_SSH_PASSWORD").strip()
    if ref.startswith("ENV:"):
        return password_from_env(ref[4:])
    if not ref.startswith("DB:"):
        return password_from_env(ref)

    sn = ref[3:].strip()
    if not sn:
        raise CaptureV2Error("CAPTURE_GATE_SSH_CREDENTIAL_REF_INVALID")
    with SessionLocal() as db:
        credential = db.scalar(select(DeviceCredential).where(DeviceCredential.sn == sn))
        if credential is None or not str(credential.password or ""):
            raise CaptureV2Error(
                "CAPTURE_GATE_SSH_CREDENTIAL_MISSING",
                details={"source": "device_credentials", "sn": sn},
            )
        return str(credential.password)


async def password_from_source_async(source: str, spec: GateDeviceSpec) -> str:
    """Resolve provider-backed credentials for Production live Gates, fail closed."""
    ref = str(source or "CAPTURE_GATE_SSH_PASSWORD").strip()
    if not ref.startswith("PROVIDER:"):
        return password_from_source(ref)

    sn = ref[len("PROVIDER:"):].strip()
    if not sn:
        raise CaptureV2Error("CAPTURE_GATE_SSH_CREDENTIAL_REF_INVALID")
    from app.integrations.credentials import get_credential_provider

    provider = get_credential_provider()
    if not bool(getattr(provider, "production_capable", False)):
        raise CaptureV2Error(
            "CAPTURE_GATE_CREDENTIAL_PROVIDER_NOT_PRODUCTION_CAPABLE",
            details={"provider": str(getattr(provider, "provider_id", type(provider).__name__))},
        )
    try:
        password = await provider.get_password(sn=sn, ip=spec.host, product=spec.model)
    except Exception as exc:
        raise CaptureV2Error(
            "CAPTURE_GATE_CREDENTIAL_PROVIDER_FAILED",
            details={
                "provider": str(getattr(provider, "provider_id", type(provider).__name__)),
                "exception": type(exc).__name__,
            },
        ) from exc
    if not str(password or ""):
        raise CaptureV2Error("CAPTURE_GATE_CREDENTIAL_PROVIDER_EMPTY_PASSWORD")
    return str(password)


class GateSftpAdapter:
    """Gate compatibility proxy for master revisions without ``sftp_get`` yet."""

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


def _adapter(spec: GateDeviceSpec, password: str):
    from app.collectors.asyncssh_adapter import AsyncSSHDeviceAdapter

    return GateSftpAdapter(
        AsyncSSHDeviceAdapter(
            ip=spec.host,
            port=int(spec.port),
            username=spec.username,
            password=password,
        )
    )


def build_asyncssh_adapter(spec: GateDeviceSpec, *, password_env: str = "CAPTURE_GATE_SSH_PASSWORD"):
    return _adapter(spec, password_from_source(password_env))


async def build_asyncssh_adapter_async(
    spec: GateDeviceSpec, *, password_env: str = "CAPTURE_GATE_SSH_PASSWORD"
):
    return _adapter(spec, await password_from_source_async(password_env, spec))
