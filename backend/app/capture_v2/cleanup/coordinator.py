from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Awaitable, Callable

from sqlalchemy import select

from app.capture_v2.db_models import CaptureEvent, CaptureSession
from app.capture_v2.enums import CaptureSessionState
from app.capture_v2.errors import CaptureV2Error
from app.core.ids import new_id


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class CleanupStep(StrEnum):
    PCM_RX_OFF = "PCM_RX_OFF"
    PCM_TX_OFF = "PCM_TX_OFF"
    DEBUG_OFF = "DEBUG_OFF"
    FXS_OBSERVER_STOP = "FXS_OBSERVER_STOP"
    GC_ACKED_SPOOL = "GC_ACKED_SPOOL"
    FINAL_RECOVERY_SCAN = "FINAL_RECOVERY_SCAN"
    RELEASE_LEASE = "RELEASE_LEASE"


_ORDER = (
    CleanupStep.PCM_RX_OFF,
    CleanupStep.PCM_TX_OFF,
    CleanupStep.DEBUG_OFF,
    CleanupStep.FXS_OBSERVER_STOP,
    CleanupStep.GC_ACKED_SPOOL,
    CleanupStep.FINAL_RECOVERY_SCAN,
    CleanupStep.RELEASE_LEASE,
)


@dataclass(frozen=True)
class CleanupStepResult:
    step: CleanupStep
    verified: bool
    details: dict


CleanupAction = Callable[[], Awaitable[CleanupStepResult | bool | None]]


class CaptureV2CleanupCoordinator:
    """Idempotent post-evidence DUT cleanup with lease release strictly last.

    Producer stop/final seal/final durable drain are Phase-C evidence finalization
    and MUST have happened before Session enters CLEANUP.  This coordinator owns
    only the remaining DUT cleanup sequence.  Every verified step is persisted so
    retry after worker crash resumes at the first unverified step.
    """

    def __init__(self, session_factory, *, actions: dict[CleanupStep, CleanupAction]):
        self.session_factory = session_factory
        self.actions = dict(actions)
        missing = [step.value for step in _ORDER if step not in self.actions]
        if missing:
            raise ValueError(f"CLEANUP_ACTIONS_MISSING:{','.join(missing)}")

    def _verified(self, capture_session_id: str) -> set[str]:
        with self.session_factory() as db:
            rows = list(db.scalars(select(CaptureEvent).where(
                CaptureEvent.capture_session_id == capture_session_id,
                CaptureEvent.event_type == "CLEANUP_STEP_VERIFIED",
            )))
            return {str((row.payload or {}).get("step")) for row in rows}

    def _session_ready(self, capture_session_id: str) -> None:
        with self.session_factory() as db:
            row = db.get(CaptureSession, capture_session_id)
            if row is None:
                raise CaptureV2Error("CAPTURE_SESSION_NOT_FOUND")
            if row.state != CaptureSessionState.CLEANUP.value:
                raise CaptureV2Error("CLEANUP_NOT_ACTIVE", details={"state": row.state})
            # Coverage may be explicit PARTIAL, so evidence_durable_at is not
            # universally required here. The transition into CLEANUP is protected
            # by RuntimeCoordinator's coverage-finalization barrier.

    def _record(self, capture_session_id: str, *, event_type: str,
                step: CleanupStep | None = None, details: dict | None = None,
                status: str | None = None) -> None:
        with self.session_factory() as db:
            with db.begin():
                row = db.get(CaptureSession, capture_session_id)
                if row is None:
                    raise CaptureV2Error("CAPTURE_SESSION_NOT_FOUND")
                if status is not None:
                    row.cleanup_status = status
                db.add(CaptureEvent(
                    id=new_id(), capture_session_id=capture_session_id,
                    entity_type="CLEANUP", entity_id=step.value if step else capture_session_id,
                    event_type=event_type, source_ts=utcnow(),
                    payload={"step": step.value if step else None, **(details or {})},
                ))

    async def run(self, *, capture_session_id: str) -> tuple[CleanupStepResult, ...]:
        self._session_ready(capture_session_id)
        verified = self._verified(capture_session_id)
        results: list[CleanupStepResult] = []
        self._record(capture_session_id, event_type="CLEANUP_STARTED", status="RUNNING")
        for step in _ORDER:
            if step.value in verified:
                results.append(CleanupStepResult(step, True, {"idempotent_replay": True}))
                continue
            # RELEASE_LEASE is unreachable until every previous step is verified
            # because the loop stops on the first failure.
            try:
                observed = await self.actions[step]()
            except CaptureV2Error as exc:
                self._record(
                    capture_session_id, event_type="CLEANUP_STEP_FAILED", step=step,
                    details={"code": exc.code, **exc.details}, status="FAILED",
                )
                raise
            except Exception as exc:
                self._record(
                    capture_session_id, event_type="CLEANUP_STEP_FAILED", step=step,
                    details={"code": "CLEANUP_STEP_EXCEPTION", "exception": type(exc).__name__},
                    status="FAILED",
                )
                raise CaptureV2Error(
                    "CLEANUP_STEP_FAILED",
                    details={"step": step.value, "exception": type(exc).__name__},
                ) from exc
            if isinstance(observed, CleanupStepResult):
                result = observed
                if result.step != step:
                    raise CaptureV2Error("CLEANUP_STEP_RESULT_MISMATCH")
            else:
                result = CleanupStepResult(step, observed is not False, {})
            if not result.verified:
                self._record(
                    capture_session_id, event_type="CLEANUP_STEP_FAILED", step=step,
                    details={"code": "CLEANUP_REVERSE_VERIFY_FAILED", **result.details}, status="FAILED",
                )
                raise CaptureV2Error(
                    "CLEANUP_REVERSE_VERIFY_FAILED", details={"step": step.value, **result.details}
                )
            self._record(
                capture_session_id, event_type="CLEANUP_STEP_VERIFIED", step=step,
                details=result.details,
            )
            verified.add(step.value)
            results.append(result)
        self._record(capture_session_id, event_type="CLEANUP_VERIFIED", status="VERIFIED")
        return tuple(results)
