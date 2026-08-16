from __future__ import annotations

import asyncio

from app.collectors.asyncssh_adapter import AsyncSSHDeviceAdapter


class _Stdin:
    def __init__(self, *, broken: bool = False):
        self.broken = broken
        self.writes: list[str] = []

    def write(self, value: str) -> None:
        if self.broken:
            raise BrokenPipeError("closed")
        self.writes.append(value)


class _Process:
    def __init__(self, *, broken: bool = False):
        self.stdin = _Stdin(broken=broken)


def test_write_aim_reopens_a_pty_that_closes_after_prompt(monkeypatch):
    adapter = AsyncSSHDeviceAdapter(
        ip="192.0.2.10", port=22, username="root", password="secret")
    adapter.conn = object()
    first = _Process(broken=True)
    second = _Process()
    processes = iter([first, second])
    closes: list[bool] = []

    async def ensure(_timeout):
        return next(processes)

    async def close():
        closes.append(True)

    monkeypatch.setattr(adapter, "_ensure_aim_session", ensure)
    monkeypatch.setattr(adapter, "_close_aim_session", close)

    asyncio.run(adapter.write_aim("debug p on"))

    assert closes == [True]
    assert second.stdin.writes == ["debug p on\n"]
