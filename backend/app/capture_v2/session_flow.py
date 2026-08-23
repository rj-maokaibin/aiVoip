from __future__ import annotations

from datetime import datetime, timezone

from app.capture_v2.db_models import CaptureEvent, CaptureSession
from app.capture_v2.enums import CaptureSessionState
from app.capture_v2.errors import CaptureV2Error
from app.core.ids import new_id


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


_ALLOWED = {
    CaptureSessionState.PREPARING: {CaptureSessionState.CAPTURE_PATH_READY},
    CaptureSessionState.CAPTURE_PATH_READY: {CaptureSessionState.WATCHING},
    CaptureSessionState.WATCHING: {
        CaptureSessionState.TARGET_CONFIRMED,
        CaptureSessionState.EVIDENCE_DRAINING,
    },
    CaptureSessionState.TARGET_CONFIRMED: {CaptureSessionState.POST_TARGET_OBSERVATION},
    CaptureSessionState.POST_TARGET_OBSERVATION: {CaptureSessionState.EVIDENCE_DRAINING},
    CaptureSessionState.EVIDENCE_DRAINING: {CaptureSessionState.COVERAGE_FINALIZING},
    CaptureSessionState.COVERAGE_FINALIZING: {CaptureSessionState.CLEANUP},
    CaptureSessionState.CLEANUP: {CaptureSessionState.COMPLETED},
}


class CaptureSessionFlow:
    """Strict/idempotent post-ownership Capture Session state machine."""

    def __init__(self, session_factory):
        self.session_factory = session_factory

    def state(self, capture_session_id: str) -> CaptureSessionState:
        with self.session_factory() as db:
            row = db.get(CaptureSession, capture_session_id)
            if row is None:
                raise CaptureV2Error("CAPTURE_SESSION_NOT_FOUND")
            return CaptureSessionState(row.state)

    def transition(self, capture_session_id: str, *, target: CaptureSessionState,
                   source_ts: datetime | None = None, reason: str | None = None,
                   payload: dict | None = None) -> CaptureSessionState:
        with self.session_factory() as db:
            with db.begin():
                row = db.get(CaptureSession, capture_session_id)
                if row is None:
                    raise CaptureV2Error("CAPTURE_SESSION_NOT_FOUND")
                current = CaptureSessionState(row.state)
                if current == target:
                    return current
                if target not in _ALLOWED.get(current, set()):
                    raise CaptureV2Error(
                        "CAPTURE_SESSION_TRANSITION_INVALID",
                        details={"from": current.value, "to": target.value},
                    )
                row.state = target.value
                ts = source_ts or utcnow()
                if target == CaptureSessionState.CAPTURE_PATH_READY:
                    row.path_ready_at = row.path_ready_at or ts
                elif target == CaptureSessionState.TARGET_CONFIRMED:
                    row.target_confirmed_at = row.target_confirmed_at or ts
                elif target == CaptureSessionState.COMPLETED:
                    row.ended_at = row.ended_at or ts
                db.add(CaptureEvent(
                    id=new_id(), capture_session_id=capture_session_id,
                    entity_type="CAPTURE_SESSION", entity_id=capture_session_id,
                    event_type=f"SESSION_{target.value}", source_ts=ts,
                    payload={"reason": reason, **(payload or {})},
                ))
                return target

    def fail(self, capture_session_id: str, *, code: str,
             source_ts: datetime | None = None, details: dict | None = None) -> None:
        with self.session_factory() as db:
            with db.begin():
                row = db.get(CaptureSession, capture_session_id)
                if row is None:
                    raise CaptureV2Error("CAPTURE_SESSION_NOT_FOUND")
                if row.state == CaptureSessionState.COMPLETED.value:
                    raise CaptureV2Error("CAPTURE_SESSION_ALREADY_COMPLETED")
                if row.state == CaptureSessionState.FAILED.value:
                    return
                row.state = CaptureSessionState.FAILED.value
                row.failure_code = code
                row.ended_at = row.ended_at or (source_ts or utcnow())
                db.add(CaptureEvent(
                    id=new_id(), capture_session_id=capture_session_id,
                    entity_type="CAPTURE_SESSION", entity_id=capture_session_id,
                    event_type="SESSION_FAILED", source_ts=source_ts or utcnow(),
                    payload={"code": code, **(details or {})},
                ))
