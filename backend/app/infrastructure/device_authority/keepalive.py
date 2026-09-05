from __future__ import annotations

import asyncio
from typing import Generic, TypeVar

from app.infrastructure.device_authority.base import DeviceAuthority


TokenT = TypeVar("TokenT")


class AuthorityKeepalive(Generic[TokenT]):
    """Keep one already-acquired DeviceAuthority term alive during long runs.

    The helper never acquires or takes over authority. It only renews the exact
    token/epoch supplied by the caller. Stop it before the final release step to
    avoid renew/release races. A renewal failure is retained and surfaced to the
    caller; it is never treated as permission to reacquire or mutate blindly.
    """

    def __init__(
        self,
        authority: DeviceAuthority[TokenT],
        *,
        interval_seconds: float = 30.0,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("AUTHORITY_KEEPALIVE_INTERVAL_INVALID")
        self.authority = authority
        self.interval_seconds = float(interval_seconds)
        self._token: TokenT | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._error: Exception | None = None

    @property
    def token(self) -> TokenT:
        if self._token is None:
            raise RuntimeError("AUTHORITY_KEEPALIVE_NOT_STARTED")
        return self._token

    @property
    def error(self) -> Exception | None:
        return self._error

    def start(self, token: TokenT) -> None:
        if self._task is not None and not self._task.done():
            raise RuntimeError("AUTHORITY_KEEPALIVE_ALREADY_STARTED")
        self._token = token
        self._error = None
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        try:
            while True:
                try:
                    await asyncio.wait_for(
                        self._stop.wait(),
                        timeout=self.interval_seconds,
                    )
                    return
                except asyncio.TimeoutError:
                    pass
                assert self._token is not None
                try:
                    self._token = self.authority.renew(self._token)
                except Exception as exc:
                    self._error = exc
                    return
        except asyncio.CancelledError:
            raise

    def raise_if_failed(self) -> None:
        if self._error is not None:
            raise RuntimeError(
                f"AUTHORITY_KEEPALIVE_FAILED:{type(self._error).__name__}"
            ) from self._error

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        self._stop.set()
        try:
            await task
        finally:
            self._task = None
        self.raise_if_failed()
