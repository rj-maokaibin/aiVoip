from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
from typing import Awaitable, Callable, Generic, TypeVar

from app.infrastructure.device_authority.base import DeviceAuthority


T = TypeVar("T")
O = TypeVar("O")
TokenT = TypeVar("TokenT")


class MutationStatus(str, Enum):
    APPLIED = "applied"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class MutationExecution(Generic[T, O]):
    status: MutationStatus
    value: T | None = None
    observation: O | None = None
    observed_after_unknown: bool = False
    error: str | None = None


class ReadOperationPolicy:
    """Bounded retry policy for side-effect-free reads."""

    def __init__(self, *, max_attempts: int = 3, backoff_seconds: float = 0.0):
        if max_attempts < 1:
            raise ValueError("READ_MAX_ATTEMPTS_INVALID")
        if backoff_seconds < 0:
            raise ValueError("READ_BACKOFF_INVALID")
        self.max_attempts = max_attempts
        self.backoff_seconds = backoff_seconds

    async def execute(
        self,
        operation: Callable[[], Awaitable[T]],
        *,
        retry_if: Callable[[Exception], bool] | None = None,
    ) -> T:
        retry_if = retry_if or (lambda exc: isinstance(exc, (TimeoutError, asyncio.TimeoutError)))
        last: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                return await operation()
            except Exception as exc:
                last = exc
                if attempt >= self.max_attempts or not retry_if(exc):
                    raise
                if self.backoff_seconds:
                    await asyncio.sleep(self.backoff_seconds * attempt)
        assert last is not None
        raise last


class MutationOperationPolicy(Generic[TokenT]):
    """Fenced mutation contract with no blind retry.

    A mutation is invoked at most once.  If its transport result is unknown, the
    policy MUST observe actual state before returning.  A caller may start a new
    policy execution only after making an explicit decision from that observation.
    """

    def __init__(self, authority: DeviceAuthority[TokenT]):
        self._authority = authority

    async def execute(
        self,
        *,
        token: TokenT,
        mutate: Callable[[], Awaitable[T]],
        observe: Callable[[], Awaitable[O]],
        is_applied: Callable[[O], bool],
        is_unknown_error: Callable[[Exception], bool] | None = None,
    ) -> MutationExecution[T, O]:
        self._authority.validate(token)
        is_unknown_error = is_unknown_error or (
            lambda exc: isinstance(exc, (TimeoutError, asyncio.TimeoutError))
        )
        try:
            value = await mutate()
            return MutationExecution(status=MutationStatus.APPLIED, value=value)
        except Exception as exc:
            if not is_unknown_error(exc):
                raise
            observation = await observe()
            if is_applied(observation):
                return MutationExecution(
                    status=MutationStatus.APPLIED,
                    observation=observation,
                    observed_after_unknown=True,
                    error=type(exc).__name__,
                )
            return MutationExecution(
                status=MutationStatus.UNKNOWN,
                observation=observation,
                observed_after_unknown=True,
                error=type(exc).__name__,
            )
