from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from app.capture_v2.db_models import CaptureEpoch
from app.capture_v2.enums import (
    CaptureEventType,
    GapCertainty,
    RecoveryClassification,
    RecoveryResultStatus,
)
from app.capture_v2.errors import CaptureV2Error
from app.capture_v2.lease.manager import LeaseToken
from app.capture_v2.producer.manager import ProducerManager
from app.capture_v2.recovery.classifier import classify_recovery
from app.capture_v2.recovery.models import (
    ActiveEpochExpectation,
    RecoveryDecision,
    RecoveryResult,
)
from app.capture_v2.recovery.scanner import RecoveryScanner
from app.capture_v2.repository.core import CaptureEpochRepository, CaptureEventRepository, CaptureGapRepository


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RecoveryManager:
    def __init__(
        self,
        *,
        session_factory: Callable,
        scanner: RecoveryScanner,
        producer_manager: ProducerManager,
    ):
        self.session_factory = session_factory
        self.scanner = scanner
        self.producer_manager = producer_manager

    def _active_epoch(self, capture_session_id: str) -> CaptureEpoch | None:
        with self.session_factory() as db:
            return CaptureEpochRepository(db).running_for_session(capture_session_id)

    @staticmethod
    def _expectation(row: CaptureEpoch | None) -> ActiveEpochExpectation | None:
        if row is None:
            return None
        return ActiveEpochExpectation(
            epoch_id=row.id,
            epoch_token=row.epoch_token,
            boot_id=row.boot_id,
            producer_pid=row.producer_pid,
            producer_starttime=row.producer_starttime,
        )

    def _event(self, capture_session_id: str, event_type: CaptureEventType, *, payload: dict) -> None:
        with self.session_factory() as db:
            with db.begin():
                CaptureEventRepository(db).append(
                    capture_session_id=capture_session_id,
                    event_type=event_type,
                    entity_type="RECOVERY",
                    source_ts=utcnow(),
                    payload=payload,
                )

    def _mark_active_ended(self, active: CaptureEpoch | None, *, reason: str, failed: bool = True) -> None:
        if active is None:
            return
        with self.session_factory() as db:
            with db.begin():
                CaptureEpochRepository(db).mark_ended(active.id, reason=reason, ended_at=utcnow(), failed=failed)

    def _open_gap(
        self,
        *,
        capture_session_id: str,
        active: CaptureEpoch | None,
        certainty: GapCertainty,
        reason_code: str,
        details: dict,
    ) -> str:
        with self.session_factory() as db:
            with db.begin():
                row = CaptureGapRepository(db).create(
                    capture_session_id=capture_session_id,
                    capture_epoch_id=active.id if active else None,
                    channel="PCAP",
                    certainty=certainty,
                    reason_code=reason_code,
                    source="RECOVERY",
                    gap_start_ts=None,
                    details={**details, "gap_start_boundary": "UNKNOWN_AT_RECOVERY"},
                )
                CaptureEventRepository(db).append(
                    capture_session_id=capture_session_id,
                    event_type=CaptureEventType.CAPTURE_GAP_START,
                    entity_type="CAPTURE_GAP",
                    entity_id=row.id,
                    source_ts=None,
                    payload={
                        "reason_code": reason_code,
                        "certainty": certainty.value,
                        "gap_start_boundary": "UNKNOWN_AT_RECOVERY",
                    },
                )
                return row.id

    async def recover(self, *, token: LeaseToken) -> RecoveryResult:
        session_id = token.capture_session_id
        self._event(session_id, CaptureEventType.RECOVERY_STARTED, payload={"lease_epoch": token.lease_epoch})
        active = self._active_epoch(session_id)
        inventory = await self.scanner.scan()
        decision = classify_recovery(
            session_id=session_id,
            inventory=inventory,
            active=self._expectation(active),
        )
        self._event(
            session_id,
            CaptureEventType.RECOVERY_CLASSIFIED,
            payload={
                "classification": decision.classification.value,
                "owned_producer_count": len(inventory.owned_producers),
                "v2_pids": [p.pid for p in inventory.v2_producers],
                "legacy_pids": [p.pid for p in inventory.legacy_producers],
                "foreign_tcpdump_pids": [p.pid for p in inventory.foreign_tcpdump],
            },
        )

        try:
            result = await self._execute_decision(token, active, decision)
        except Exception as exc:
            self._event(
                session_id,
                CaptureEventType.RECOVERY_FAILED,
                payload={"classification": decision.classification.value, "error": type(exc).__name__},
            )
            raise

        final_inventory = await self.scanner.scan()
        if len(final_inventory.owned_producers) > 1:
            self._event(
                session_id,
                CaptureEventType.RECOVERY_FAILED,
                payload={"reason": "POST_RECOVERY_MULTIPLE_PRODUCERS"},
            )
            raise CaptureV2Error(
                "RECOVERY_FAILED",
                details={"producer_count": len(final_inventory.owned_producers)},
            )
        self._event(
            session_id,
            CaptureEventType.RECOVERY_COMPLETED,
            payload={"status": result.status.value, "producer_count": len(final_inventory.owned_producers)},
        )
        return result

    async def _execute_decision(
        self,
        token: LeaseToken,
        active: CaptureEpoch | None,
        decision: RecoveryDecision,
    ) -> RecoveryResult:
        cls = decision.classification
        session_id = token.capture_session_id

        if cls == RecoveryClassification.CLEAN:
            return RecoveryResult(RecoveryResultStatus.CLEAN, cls)

        if cls == RecoveryClassification.SAME_SESSION_ALIVE:
            assert decision.current is not None
            producer = await self.producer_manager.adopt(decision.current)
            self._event(
                session_id,
                CaptureEventType.RECOVERY_ADOPTED,
                payload={
                    "pid": producer.pid,
                    "starttime": producer.process_starttime,
                    "capture_epoch": producer.capture_epoch,
                },
            )
            return RecoveryResult(RecoveryResultStatus.ADOPTED, cls, producer=producer)

        if cls == RecoveryClassification.SAME_SESSION_DEAD:
            self._mark_active_ended(active, reason="PRODUCER_MISSING_DURING_RECOVERY", failed=True)
            gap_id = self._open_gap(
                capture_session_id=session_id,
                active=active,
                certainty=GapCertainty.POSSIBLE,
                reason_code="PCAP_PRODUCER_GAP",
                details={"reason": decision.reason, "detected_during": "RECOVERY"},
            )
            self._event(session_id, CaptureEventType.PRODUCER_DIED, payload={"capture_epoch": active.epoch_token if active else None})
            return RecoveryResult(RecoveryResultStatus.REPAIRED, cls, gaps_created=(gap_id,))

        if cls == RecoveryClassification.OLD_SESSION_ALIVE:
            stopped = []
            for producer in decision.stale:
                self._event(
                    session_id,
                    CaptureEventType.RECOVERY_ORPHAN_FOUND,
                    payload={"pid": producer.pid, "legacy": producer.legacy, "output_path": producer.output_path},
                )
                await self.producer_manager.stop_identity(token, producer)
                stopped.append(producer)
                self._event(
                    session_id,
                    CaptureEventType.PRODUCER_STOPPED,
                    payload={
                        "pid": producer.pid,
                        "starttime": producer.process_starttime,
                        "capture_epoch": producer.capture_epoch,
                        "legacy": producer.legacy,
                        "reason": "RECOVERY_STALE_PRODUCER",
                        "lease_epoch": token.lease_epoch,
                    },
                )

            # If DB says this CaptureSession already had an active epoch, the single
            # process we just stopped was *not* that expected producer. Therefore the
            # expected producer disappeared at an unknown time. Stopping the orphan
            # without opening a gap would silently claim continuity.
            gap_ids: tuple[str, ...] = ()
            if active is not None:
                self._mark_active_ended(active, reason="EXPECTED_PRODUCER_MISSING_WITH_ORPHAN", failed=True)
                gap_id = self._open_gap(
                    capture_session_id=session_id,
                    active=active,
                    certainty=GapCertainty.POSSIBLE,
                    reason_code="PCAP_PRODUCER_GAP",
                    details={
                        "reason": "ACTIVE_EPOCH_PROCESS_MISSING_WITH_STALE_PRODUCER",
                        "stopped_pids": [p.pid for p in stopped],
                    },
                )
                self._event(
                    session_id,
                    CaptureEventType.PRODUCER_DIED,
                    payload={"capture_epoch": active.epoch_token},
                )
                gap_ids = (gap_id,)
            return RecoveryResult(
                RecoveryResultStatus.REPAIRED,
                cls,
                stopped=tuple(stopped),
                gaps_created=gap_ids,
            )

        if cls == RecoveryClassification.MULTIPLE_PRODUCERS:
            self._event(
                session_id,
                CaptureEventType.RECOVERY_CONFLICT_FOUND,
                payload={
                    "current_pid": decision.current.pid if decision.current else None,
                    "stale_pids": [p.pid for p in decision.stale],
                },
            )
            stopped = []
            for producer in decision.stale:
                await self.producer_manager.stop_identity(token, producer)
                stopped.append(producer)
                self._event(
                    session_id,
                    CaptureEventType.PRODUCER_STOPPED,
                    payload={
                        "pid": producer.pid,
                        "starttime": producer.process_starttime,
                        "capture_epoch": producer.capture_epoch,
                        "legacy": producer.legacy,
                        "reason": "RECOVERY_STALE_PRODUCER",
                        "lease_epoch": token.lease_epoch,
                    },
                )
            if decision.current is not None:
                current = await self.producer_manager.adopt(decision.current)
                return RecoveryResult(
                    RecoveryResultStatus.CONFLICT_RESOLVED,
                    cls,
                    producer=current,
                    stopped=tuple(stopped),
                )
            gap_ids: tuple[str, ...] = ()
            if active is not None:
                self._mark_active_ended(active, reason="MULTIPLE_PRODUCER_AMBIGUOUS", failed=True)
                gap_id = self._open_gap(
                    capture_session_id=session_id,
                    active=active,
                    certainty=GapCertainty.CONFIRMED,
                    reason_code="CAPTURE_CONFLICT_GAP",
                    details={"stopped_pids": [p.pid for p in stopped]},
                )
                gap_ids = (gap_id,)
            return RecoveryResult(
                RecoveryResultStatus.CONFLICT_RESOLVED,
                cls,
                stopped=tuple(stopped),
                gaps_created=gap_ids,
            )

        if cls == RecoveryClassification.DUT_REBOOT:
            self._mark_active_ended(active, reason="DUT_REBOOT", failed=True)
            gap_id = self._open_gap(
                capture_session_id=session_id,
                active=active,
                certainty=GapCertainty.POSSIBLE,
                reason_code="DUT_REBOOT_GAP",
                details={"reason": decision.reason},
            )
            self._event(
                session_id,
                CaptureEventType.DUT_REBOOT_DETECTED,
                payload={"old_boot_id": active.boot_id if active else None},
            )
            # /tmp normally vanished, but stop any discovered aiVoip-owned process defensively.
            stopped = []
            for producer in decision.stale:
                await self.producer_manager.stop_identity(token, producer)
                stopped.append(producer)
                self._event(
                    session_id,
                    CaptureEventType.PRODUCER_STOPPED,
                    payload={
                        "pid": producer.pid,
                        "starttime": producer.process_starttime,
                        "capture_epoch": producer.capture_epoch,
                        "legacy": producer.legacy,
                        "reason": "RECOVERY_STALE_PRODUCER",
                        "lease_epoch": token.lease_epoch,
                    },
                )
            return RecoveryResult(
                RecoveryResultStatus.REPAIRED,
                cls,
                stopped=tuple(stopped),
                gaps_created=(gap_id,),
            )

        raise CaptureV2Error("RECOVERY_CLASSIFICATION_UNSUPPORTED", details={"classification": cls.value})
