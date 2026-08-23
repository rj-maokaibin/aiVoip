from __future__ import annotations

from dataclasses import dataclass, field

from app.capture_v2.enums import RecoveryClassification, RecoveryResultStatus
from app.capture_v2.producer.identity import ProducerIdentity


@dataclass(frozen=True)
class RecoveryInventory:
    boot_id: str
    control_lease_epoch: int | None
    control_session_id: str | None
    control_boot_id: str | None
    v2_producers: tuple[ProducerIdentity, ...] = ()
    legacy_producers: tuple[ProducerIdentity, ...] = ()
    foreign_tcpdump: tuple[ProducerIdentity, ...] = ()
    epoch_dirs: tuple[str, ...] = ()
    legacy_ring_dirs: tuple[str, ...] = ()

    @property
    def owned_producers(self) -> tuple[ProducerIdentity, ...]:
        return self.v2_producers + self.legacy_producers


@dataclass(frozen=True)
class ActiveEpochExpectation:
    epoch_id: str
    epoch_token: str
    boot_id: str | None
    producer_pid: int | None
    producer_starttime: int | None


@dataclass(frozen=True)
class RecoveryDecision:
    classification: RecoveryClassification
    current: ProducerIdentity | None = None
    stale: tuple[ProducerIdentity, ...] = ()
    reason: str | None = None


@dataclass(frozen=True)
class RecoveryResult:
    status: RecoveryResultStatus
    classification: RecoveryClassification
    producer: ProducerIdentity | None = None
    stopped: tuple[ProducerIdentity, ...] = ()
    gaps_created: tuple[str, ...] = ()
    details: dict = field(default_factory=dict)
