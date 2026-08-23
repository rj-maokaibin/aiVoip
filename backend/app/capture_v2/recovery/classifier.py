from __future__ import annotations

from app.capture_v2.enums import RecoveryClassification
from app.capture_v2.recovery.models import ActiveEpochExpectation, RecoveryDecision, RecoveryInventory


def _matches_expected(producer, *, session_id: str, active: ActiveEpochExpectation | None) -> bool:
    if producer.session_id != session_id:
        return False
    if active is None:
        return False
    if producer.capture_epoch != active.epoch_token:
        return False
    if active.producer_pid is not None and producer.pid != active.producer_pid:
        return False
    if active.producer_starttime is not None and producer.process_starttime != active.producer_starttime:
        return False
    return True


def classify_recovery(
    *,
    session_id: str,
    inventory: RecoveryInventory,
    active: ActiveEpochExpectation | None,
) -> RecoveryDecision:
    owned = inventory.owned_producers

    if active is not None and active.boot_id and active.boot_id != inventory.boot_id:
        return RecoveryDecision(
            RecoveryClassification.DUT_REBOOT,
            stale=owned,
            reason="BOOT_ID_CHANGED",
        )

    if len(owned) > 1:
        matching = tuple(p for p in owned if _matches_expected(p, session_id=session_id, active=active))
        current = matching[0] if len(matching) == 1 else None
        stale = tuple(p for p in owned if current is None or p != current)
        return RecoveryDecision(
            RecoveryClassification.MULTIPLE_PRODUCERS,
            current=current,
            stale=stale,
            reason="MULTIPLE_AIVOIP_PRODUCERS",
        )

    if len(owned) == 1:
        producer = owned[0]
        if _matches_expected(producer, session_id=session_id, active=active):
            return RecoveryDecision(RecoveryClassification.SAME_SESSION_ALIVE, current=producer)
        return RecoveryDecision(
            RecoveryClassification.OLD_SESSION_ALIVE,
            stale=(producer,),
            reason="STALE_OR_LEGACY_PRODUCER",
        )

    if active is not None:
        return RecoveryDecision(
            RecoveryClassification.SAME_SESSION_DEAD,
            reason="ACTIVE_EPOCH_PROCESS_MISSING",
        )

    return RecoveryDecision(RecoveryClassification.CLEAN)
