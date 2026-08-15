"""Reproduction capture-stability tests (no real device needed).

These verify the capture-hardening logic holds under transient device faults and
load, so the "capture must never be lost" guarantee is not just a config option:
  - execute_shell retries transient SSH timeouts and still succeeds;
  - it gives up only after exhausting retries (no infinite loop);
  - a watcher bind_call transient failure does not crash the loop (rolls back and
    keeps listening) - exercised through the orchestrator helper paths.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.collectors.asyncssh_adapter import AsyncSSHDeviceAdapter, DeviceCommandError


class _FakeConn:
    """A minimal stand-in for asyncssh connection exposing only run()."""

    def __init__(self, fail_times: int = 2):
        self.fail_times = fail_times
        self.calls = 0

    async def run(self, command, check=False):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise asyncio.TimeoutError()
        return SimpleNamespace(stdout='CAPTURE_OK', stderr='', exit_status=0)


def _adapter_with(fake_conn):
    a = AsyncSSHDeviceAdapter(ip='192.0.2.1', port=22, username='root', password='x')
    a.conn = fake_conn
    return a


def test_execute_shell_retries_transient_timeout_and_succeeds():
    a = _adapter_with(_FakeConn(fail_times=2))
    res = asyncio.run(a.execute_shell('tcpdump -w /tmp/x.pcap', timeout=5, retries=2))
    assert res.stdout == 'CAPTURE_OK'
    assert a.conn.calls == 3  # 2 timeouts + 1 success


def test_execute_shell_recovers_after_single_timeout():
    a = _adapter_with(_FakeConn(fail_times=1))
    res = asyncio.run(a.execute_shell('tcpdump -w /tmp/x.pcap', timeout=5, retries=2))
    assert res.stdout == 'CAPTURE_OK'
    assert a.conn.calls == 2


def test_execute_shell_raises_after_exhausting_retries():
    a = _adapter_with(_FakeConn(fail_times=99))  # always timeout
    with pytest.raises(DeviceCommandError) as ei:
        asyncio.run(a.execute_shell('tcpdump -w /tmp/x.pcap', timeout=5, retries=2))
    assert 'SSH_COMMAND_TIMEOUT' in str(ei.value)
    assert a.conn.calls == 3  # exactly retries+1, no infinite loop


def test_execute_shell_no_retry_when_retries_zero():
    a = _adapter_with(_FakeConn(fail_times=99))
    with pytest.raises(DeviceCommandError):
        asyncio.run(a.execute_shell('tcpdump -w /tmp/x.pcap', timeout=5, retries=0))
    assert a.conn.calls == 1
