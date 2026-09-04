from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class NormalizedEvent:
    name: str
    payload: dict[str, Any]
    source_timestamp: datetime
    evidence_refs: tuple[str, ...] = ()


class EventWaitTimeout(TimeoutError):
    pass


class InMemoryEventBus:
    """V1 normalized event adapter; producers publish facts, waiters never parse raw logs."""

    def __init__(self) -> None:
        self._events: list[NormalizedEvent] = []
        self._condition = asyncio.Condition()

    async def publish(self, event: NormalizedEvent) -> None:
        async with self._condition:
            self._events.append(event)
            self._condition.notify_all()

    async def emit(
        self,
        name: str,
        payload: dict[str, Any] | None = None,
        *,
        evidence_refs: tuple[str, ...] = (),
        source_timestamp: datetime | None = None,
    ) -> NormalizedEvent:
        event = NormalizedEvent(
            name=name,
            payload=dict(payload or {}),
            source_timestamp=source_timestamp or utcnow(),
            evidence_refs=tuple(evidence_refs),
        )
        await self.publish(event)
        return event

    async def wait_for(
        self,
        name: str,
        *,
        timeout: float,
        predicate: Callable[[NormalizedEvent], bool] | None = None,
    ) -> NormalizedEvent:
        if timeout <= 0:
            raise ValueError("EVENT_TIMEOUT_MUST_BE_POSITIVE")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        seen = 0
        async with self._condition:
            while True:
                for event in self._events[seen:]:
                    if event.name == name and (predicate is None or predicate(event)):
                        return event
                seen = len(self._events)
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise EventWaitTimeout(f"EVENT_WAIT_TIMEOUT:{name}")
                try:
                    await asyncio.wait_for(self._condition.wait(), timeout=remaining)
                except asyncio.TimeoutError as exc:
                    raise EventWaitTimeout(f"EVENT_WAIT_TIMEOUT:{name}") from exc
