"""Reproduction platform factory.

Selects the orchestrator's platform based on ``REPRODUCTION_PLATFORM_MODE``:

- ``mock`` (default, development/CI): ``MockReproductionPlatform``; no device I/O.
- ``real``: ``RealReproductionPlatform`` driven through an ``AsyncSSHDeviceAdapter`` to the
  EC-02 DUT, plus a transport-injected ``PcmCleanupGuard`` so PCM OFF is guarded and never
  repeated. The returned orchestrator's FXS event monitor is wired by the caller (the
  watcher task), which keeps this factory free of long-lived connection ownership.

``build_orchestrator`` returns ``(orchestrator, close)`` where ``close`` must be invoked by
the caller once the session work is done so the background transport loop shuts down.
"""
from __future__ import annotations

from typing import Callable

from app.core.config import settings
from app.reproduction.orchestrator import ReproductionOrchestrator


class PlatformNotConfigured(RuntimeError):
    pass


def resolve_platform_mode() -> str:
    return str(settings.reproduction_platform_mode or 'mock').lower().strip()


def build_orchestrator(*, adapter=None, password: str | None = None, connect: bool = True):
    """Construct an orchestrator for the configured platform mode.

    ``adapter`` may be injected (tests); otherwise a real ``AsyncSSHDeviceAdapter`` is
    built but only connected when ``connect`` is true. Returns ``(orchestrator, close)``.
    """
    mode = resolve_platform_mode()
    if mode == 'mock':
        return ReproductionOrchestrator(), (lambda: None)

    from app.reproduction.real_platform import RealReproductionPlatform
    from app.reproduction.pcm_cleanup import PcmCleanupGuard

    if adapter is None:
        raise PlatformNotConfigured('REAL_PLATFORM_ADAPTER_REQUIRED')

    platform = RealReproductionPlatform(adapter=adapter)
    guard = PcmCleanupGuard(
        probe_packets=platform._probe_packets,
        execute_aim=platform._execute_aim,
    )
    orch = ReproductionOrchestrator(platform=platform, pcm_cleanup_guard=guard)
    if connect:
        # The adapter is async; connecting happens on the platform's bridge loop.
        platform._bridge.run(adapter.connect())

    def _close():
        # Best-effort disconnect on the same bridge loop.
        try:
            platform._bridge.run(adapter.disconnect())
        except Exception:
            pass

    return orch, _close
