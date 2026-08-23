"""Reproduction platform factory.

The business-state orchestrator is shared by V1 and V2.  In real mode, the long-
lived capture platform is selected by Capture Engine authority: V1 uses the legacy
RealReproductionPlatform; V2 uses CaptureV2ProductionPlatform, which keeps the
same watcher contract but delegates PCAP ownership/segments/fencing to Capture V2.
"""
from __future__ import annotations

from app.capture_v2.runtime import (
    assert_selected_v2_live_capture_allowed,
    capture_v2_enabled,
)
from app.core.config import settings
from app.reproduction.orchestrator import ReproductionOrchestrator


class PlatformNotConfigured(RuntimeError):
    pass


def resolve_platform_mode() -> str:
    return str(settings.reproduction_platform_mode or 'mock').lower().strip()


def build_orchestrator(
    *,
    adapter=None,
    password: str | None = None,
    connect: bool = True,
    force_legacy_platform: bool = False,
):
    """Construct the configured business orchestrator and capture platform.

    ``force_legacy_platform`` is used only by the short-lived ``reproduction.start``
    semantic/arm phase.  Real long-lived watcher calls leave it false; if V2 is
    selected they are fail-closed through the release/rehearsal gate and receive a
    CaptureV2ProductionPlatform.  The start phase never creates V2 lease ownership.
    """
    del password
    mode = resolve_platform_mode()
    if mode == 'mock':
        return ReproductionOrchestrator(), (lambda: None)

    if adapter is None:
        raise PlatformNotConfigured('REAL_PLATFORM_ADAPTER_REQUIRED')

    from app.reproduction.pcm_cleanup import PcmCleanupGuard

    if capture_v2_enabled() and not force_legacy_platform:
        assert_selected_v2_live_capture_allowed()
        from app.capture_v2.production_platform import CaptureV2ProductionPlatform
        platform = CaptureV2ProductionPlatform(adapter=adapter)
        guard = platform.pcm_cleanup_guard
    else:
        from app.reproduction.real_platform import RealReproductionPlatform
        platform = RealReproductionPlatform(adapter=adapter)
        guard = PcmCleanupGuard(
            probe_packets=platform._probe_packets,
            execute_aim=platform._execute_aim,
        )

    orch = ReproductionOrchestrator(platform=platform, pcm_cleanup_guard=guard)
    if connect:
        platform.connect()

    def _close():
        platform.disconnect()

    return orch, _close
