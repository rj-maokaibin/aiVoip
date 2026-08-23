from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable
from uuid import uuid4

from app.capture_v2.enums import CaptureEventType, CaptureHealth, CaptureSessionState
from app.capture_v2.errors import CaptureV2Error
from app.capture_v2.lease.manager import CaptureLeaseManager, LeaseToken
from app.capture_v2.producer.identity import ProducerIdentity
from app.capture_v2.producer.manager import ProducerManager, ProducerStartSpec
from app.capture_v2.profiles.schema import EffectiveCaptureProfile
from app.capture_v2.recovery.manager import RecoveryManager
from app.capture_v2.recovery.models import RecoveryResult
from app.capture_v2.repository.core import (
    CaptureEpochRepository,
    CaptureEventRepository,
    CaptureGapRepository,
    CaptureSessionRepository,
)
from app.capture_v2.transport.mutator import FencedDeviceMutator
from app.capture_v2.transport.readonly import ReadOnlyDeviceTransport


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class OwnershipReady:
    lease: LeaseToken
    producer: ProducerIdentity
    capture_epoch_id: str
    capture_epoch_token: str
    recovery: RecoveryResult


class CaptureSupervisorV2:
    """Phase A/B supervisor: establish fenced ownership and exactly one producer.

    CAPTURE_PATH_READY is intentionally NOT emitted here. PCM/FXS/Server-store
    readiness belongs to Phase C/D. Successful A/B ends in PREPARING.
    """

    def __init__(
        self,
        *,
        session_factory: Callable,
        lease_manager: CaptureLeaseManager,
        reader: ReadOnlyDeviceTransport,
        mutator: FencedDeviceMutator,
        recovery_manager: RecoveryManager,
        producer_manager: ProducerManager,
    ):
        self.session_factory = session_factory
        self.lease_manager = lease_manager
        self.reader = reader
        self.mutator = mutator
        self.recovery_manager = recovery_manager
        self.producer_manager = producer_manager

    def create_session(
        self,
        *,
        reproduction_session_id: str,
        device_id: str,
        effective_profile: EffectiveCaptureProfile,
    ) -> str:
        with self.session_factory() as db:
            with db.begin():
                repo = CaptureSessionRepository(db)
                row = repo.create(
                    reproduction_session_id=reproduction_session_id,
                    device_id=device_id,
                    capture_profile_id=effective_profile.capture_profile_id,
                    capture_profile_version=effective_profile.capture_profile_version,
                    platform_profile_id=effective_profile.platform_profile_id,
                    platform_profile_version=effective_profile.platform_profile_version,
                    effective_profile=effective_profile.model_dump(mode="json"),
                    state=CaptureSessionState.CREATED.value,
                    health_status=CaptureHealth.HEALTHY.value,
                )
                CaptureEventRepository(db).append(
                    capture_session_id=row.id,
                    event_type=CaptureEventType.CAPTURE_SESSION_CREATED,
                    entity_type="CAPTURE_SESSION",
                    entity_id=row.id,
                    source_ts=utcnow(),
                    payload={"profile_checksum": effective_profile.checksum_sha256},
                )
                return row.id

    def _transition(self, session_id: str, expected: CaptureSessionState, next_state: CaptureSessionState) -> None:
        with self.session_factory() as db:
            with db.begin():
                CaptureSessionRepository(db).transition(
                    session_id,
                    expected=expected.value,
                    next_state=next_state.value,
                )

    def _create_epoch(
        self,
        *,
        capture_session_id: str,
        device_id: str,
        interface: str,
        boot_id: str,
        lease_epoch: int,
    ) -> tuple[str, str]:
        with self.session_factory() as db:
            with db.begin():
                repo = CaptureEpochRepository(db)
                index = repo.next_index(capture_session_id)
                token = f"CAP_{capture_session_id[:8]}_{index:04d}_{uuid4().hex[:8]}"
                row = repo.create_starting(
                    capture_session_id=capture_session_id,
                    device_id=device_id,
                    epoch_index=index,
                    epoch_token=token,
                    boot_id=boot_id,
                    interface=interface,
                    lease_epoch=lease_epoch,
                )
                CaptureEventRepository(db).append(
                    capture_session_id=capture_session_id,
                    event_type=CaptureEventType.CAPTURE_EPOCH_STARTED,
                    entity_type="CAPTURE_EPOCH",
                    entity_id=row.id,
                    source_ts=utcnow(),
                    payload={"epoch_token": token, "epoch_index": index, "lease_epoch": lease_epoch},
                )
                return row.id, token

    def _mark_epoch_running(self, capture_session_id: str, epoch_id: str, producer: ProducerIdentity) -> None:
        with self.session_factory() as db:
            with db.begin():
                CaptureEpochRepository(db).mark_running(
                    epoch_id,
                    pid=producer.pid,
                    starttime=producer.process_starttime,
                    cmdline=producer.cmdline,
                )
                CaptureEventRepository(db).append(
                    capture_session_id=capture_session_id,
                    event_type=CaptureEventType.PRODUCER_READY,
                    entity_type="CAPTURE_EPOCH",
                    entity_id=epoch_id,
                    source_ts=utcnow(),
                    payload={
                        "pid": producer.pid,
                        "starttime": producer.process_starttime,
                        "interface": producer.interface,
                        "capture_epoch": producer.capture_epoch,
                    },
                )

    def _running_epoch_identity(self, capture_session_id: str) -> tuple[str, str] | None:
        with self.session_factory() as db:
            row = CaptureEpochRepository(db).running_for_session(capture_session_id)
            if row is None:
                return None
            return row.id, row.epoch_token

    def _close_recovery_gaps(self, capture_session_id: str, gap_ids: tuple[str, ...]) -> None:
        if not gap_ids:
            return
        now = utcnow()
        with self.session_factory() as db:
            with db.begin():
                gaps = CaptureGapRepository(db)
                events = CaptureEventRepository(db)
                for gap_id in gap_ids:
                    gaps.close(gap_id, gap_end_ts=now, recovered_at=now)
                    events.append(
                        capture_session_id=capture_session_id,
                        event_type=CaptureEventType.CAPTURE_GAP_END,
                        entity_type="CAPTURE_GAP",
                        entity_id=gap_id,
                        source_ts=now,
                    )

    def _recoverable_state(self, capture_session_id: str) -> CaptureSessionState:
        """Read the business state without mutating it before ownership is won.

        A contending worker which loses LEASE_BUSY must be unable to perturb the
        active owner's CaptureSession state. The actual transition into
        ACQUIRING_LEASE happens only after DB lease acquisition succeeds.
        """
        with self.session_factory() as db:
            row = CaptureSessionRepository(db).get(capture_session_id)
            if row is None:
                raise CaptureV2Error("CAPTURE_SESSION_NOT_FOUND")
            current = CaptureSessionState(row.state)
        if current in {CaptureSessionState.COMPLETED, CaptureSessionState.FAILED, CaptureSessionState.CLEANUP}:
            raise CaptureV2Error("CAPTURE_SESSION_NOT_RECOVERABLE", details={"state": current.value})
        return current

    def _enter_ownership_recovery(
        self, capture_session_id: str, resume_state: CaptureSessionState
    ) -> None:
        if resume_state != CaptureSessionState.ACQUIRING_LEASE:
            # CAS protects against a business-state change which raced with lease
            # acquisition. If it moved, fail closed and let a fresh attempt re-read.
            self._transition(capture_session_id, resume_state, CaptureSessionState.ACQUIRING_LEASE)

    async def establish_ownership(
        self,
        *,
        capture_session_id: str,
        device_id: str,
        worker_id: str,
        voice_interface: str,
    ) -> OwnershipReady:
        # Worker restart/takeover is allowed on the same CaptureSession. Preserve the
        # business state, temporarily enter ownership recovery, then restore it after
        # adopting the same producer whenever continuity is intact.
        resume_state = self._recoverable_state(capture_session_id)
        # Acquire authority before changing shared CaptureSession state. A loser
        # receiving LEASE_BUSY leaves the running owner's state untouched.
        token = self.lease_manager.acquire(
            device_id=device_id,
            capture_session_id=capture_session_id,
            owner_worker_id=worker_id,
        )
        try:
            self._enter_ownership_recovery(capture_session_id, resume_state)
            boot_id = await self.reader.boot_id()
            await self.mutator.publish_fence(token, boot_id=boot_id)
            with self.session_factory() as db:
                with db.begin():
                    CaptureEventRepository(db).append(
                        capture_session_id=capture_session_id,
                        event_type=CaptureEventType.DUT_FENCE_PUBLISHED,
                        entity_type="CAPTURE_LEASE",
                        entity_id=device_id,
                        source_ts=utcnow(),
                        payload={
                            "lease_epoch": token.lease_epoch,
                            "owner_worker_id": worker_id,
                            "boot_id": boot_id,
                        },
                    )
            self._transition(
                capture_session_id,
                CaptureSessionState.ACQUIRING_LEASE,
                CaptureSessionState.RECOVERING,
            )
            recovery = await self.recovery_manager.recover(token=token)
            producer = recovery.producer

            if producer is None:
                epoch_id, epoch_token = self._create_epoch(
                    capture_session_id=capture_session_id,
                    device_id=device_id,
                    interface=voice_interface,
                    boot_id=boot_id,
                    lease_epoch=token.lease_epoch,
                )
                producer = await self.producer_manager.start(
                    token,
                    ProducerStartSpec(
                        capture_epoch=epoch_token,
                        session_id=capture_session_id,
                        interface=voice_interface,
                    ),
                )
                self._mark_epoch_running(capture_session_id, epoch_id, producer)
                self._close_recovery_gaps(capture_session_id, recovery.gaps_created)
            else:
                running = self._running_epoch_identity(capture_session_id)
                if running is None:
                    raise CaptureV2Error("RECOVERY_ADOPTED_WITHOUT_DB_EPOCH")
                epoch_id, epoch_token = running

            owned = await self.producer_manager.inspect_owned()
            if len(owned) != 1:
                raise CaptureV2Error(
                    "EXACTLY_ONE_PRODUCER_INVARIANT_FAILED",
                    details={"producer_count": len(owned), "pids": [p.pid for p in owned]},
                )
            restore_state = (
                CaptureSessionState.PREPARING
                if resume_state in {
                    CaptureSessionState.CREATED,
                    CaptureSessionState.ACQUIRING_LEASE,
                    CaptureSessionState.RECOVERING,
                    CaptureSessionState.PREPARING,
                }
                else resume_state
            )
            self._transition(
                capture_session_id,
                CaptureSessionState.RECOVERING,
                restore_state,
            )
            return OwnershipReady(
                lease=token,
                producer=producer,
                capture_epoch_id=epoch_id,
                capture_epoch_token=epoch_token,
                recovery=recovery,
            )
        except Exception:
            # Never stop capture merely because control setup failed. Release DB authority
            # so another worker can acquire a higher epoch and recover/adopt safely.
            try:
                self.lease_manager.release(token)
            except Exception:
                pass
            raise
