from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from app.core.config import settings
from app.reproduction.platform_factory import build_orchestrator, resolve_platform_mode
from app.reproduction.real_platform import RealReproductionPlatform
from app.reproduction.mock_platform import MockReproductionPlatform


@dataclass
class FakeAdapter:
    connected: bool = False
    shell_calls: list = field(default_factory=list)

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False

    async def execute_shell(self, command: str, timeout: float | None = None):
        self.shell_calls.append(command)
        from app.collectors.device_adapter import CommandResult
        return CommandResult(stdout='')

    async def execute_cli(self, command: str, timeout: float | None = None):
        from app.collectors.device_adapter import CommandResult
        return CommandResult(stdout='AIM>')


@pytest.fixture
def mode(monkeypatch):
    def _set(value: str):
        monkeypatch.setattr(settings, 'reproduction_platform_mode', value)
    return _set


def test_resolve_platform_mode_defaults_to_mock():
    assert resolve_platform_mode() == 'mock'


def test_mock_mode_returns_mock_platform(mode):
    mode('mock')
    orch, close = build_orchestrator(connect=False)
    try:
        assert isinstance(orch.platform, MockReproductionPlatform)
    finally:
        close()


def test_real_mode_returns_real_platform_and_connects(mode):
    mode('real')
    adapter = FakeAdapter()
    orch, close = build_orchestrator(adapter=adapter, connect=True)
    try:
        assert isinstance(orch.platform, RealReproductionPlatform)
        assert adapter.connected is True
        # Real mode binds a PCM cleanup guard to the platform's transport hooks.
        assert orch.pcm_cleanup_guard is not None
    finally:
        close()
    assert adapter.connected is False


def test_real_mode_requires_adapter(mode):
    mode('real')
    from app.reproduction.platform_factory import PlatformNotConfigured
    with pytest.raises(PlatformNotConfigured):
        build_orchestrator(connect=False)
