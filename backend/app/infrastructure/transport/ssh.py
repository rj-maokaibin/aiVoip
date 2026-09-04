from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.collectors.device_adapter import CommandResult


@runtime_checkable
class SshAdapterProtocol(Protocol):
    """The subset of AsyncSSHDeviceAdapter shared by automation domains."""

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def execute_shell(
        self,
        command: str,
        timeout: float | None = None,
        retries: int = 2,
    ) -> CommandResult: ...

    async def execute_cli(self, command: str, timeout: float | None = None) -> CommandResult: ...

    async def sftp_get(self, remote_path: str, local_path: str, timeout: float | None = None) -> None: ...

    async def scp_get(self, remote_path: str, local_path: str, timeout: float | None = None) -> None: ...


class SharedSshTransport:
    """Facade over the existing AsyncSSHDeviceAdapter; it owns no SSH client.

    Retry choice is explicit at each call site.  Mutation callers MUST use
    retries=0; the mutation contract enforces observe-before-retry separately.
    """

    def __init__(self, adapter: SshAdapterProtocol):
        self._adapter = adapter

    @property
    def adapter(self) -> SshAdapterProtocol:
        return self._adapter

    async def connect(self) -> None:
        await self._adapter.connect()

    async def disconnect(self) -> None:
        await self._adapter.disconnect()

    async def execute(
        self,
        command: str,
        *,
        timeout: float | None = None,
        retries: int = 0,
    ) -> CommandResult:
        return await self._adapter.execute_shell(command, timeout=timeout, retries=retries)

    async def execute_cli(self, command: str, *, timeout: float | None = None) -> CommandResult:
        return await self._adapter.execute_cli(command, timeout=timeout)

    async def sftp_get(self, remote_path: str, local_path: str, *, timeout: float | None = None) -> None:
        await self._adapter.sftp_get(remote_path, local_path, timeout=timeout)

    async def scp_get(self, remote_path: str, local_path: str, *, timeout: float | None = None) -> None:
        await self._adapter.scp_get(remote_path, local_path, timeout=timeout)
