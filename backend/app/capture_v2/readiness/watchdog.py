from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WatchdogInputs:
    lease_active: bool
    producer_alive: bool
    producer_count: int
    fxs_reader_alive: bool
    server_store_healthy: bool
    transfer_healthy: bool
    spool_critical: bool


@dataclass(frozen=True)
class WatchdogDecision:
    healthy: bool
    reasons: tuple[str, ...]


class CaptureWatchdog:
    @staticmethod
    def evaluate(values: WatchdogInputs) -> WatchdogDecision:
        reasons = []
        if not values.lease_active:
            reasons.append("LEASE_NOT_ACTIVE")
        if not values.producer_alive:
            reasons.append("PRODUCER_NOT_ALIVE")
        if values.producer_count != 1:
            reasons.append("PRODUCER_COUNT_INVALID")
        if not values.fxs_reader_alive:
            reasons.append("FXS_READER_NOT_ALIVE")
        if not values.server_store_healthy:
            reasons.append("SERVER_STORE_UNHEALTHY")
        if not values.transfer_healthy:
            reasons.append("TRANSFER_UNHEALTHY")
        if values.spool_critical:
            reasons.append("SPOOL_CRITICAL")
        return WatchdogDecision(not reasons, tuple(reasons))
