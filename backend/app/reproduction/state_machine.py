from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.contracts.enums import EventType, ReproductionEvent, ReproductionState
from app.core.errors import AppError
from app.db.models import ReproductionEventRecord, ReproductionSession
from app.services.audit import audit


def _utcnow():
    return datetime.now(timezone.utc)


# Frozen M6.2 state machine. Business code must not assign session.state directly.
_TRANSITIONS: dict[ReproductionState, dict[ReproductionEvent, ReproductionState]] = {
    ReproductionState.CREATED: {
        ReproductionEvent.START_ARMING: ReproductionState.AUTO_ARMING,
        ReproductionEvent.DEVICE_RESOURCE_BUSY: ReproductionState.WAITING_DEVICE_RESOURCE,
        ReproductionEvent.GLOBAL_RESOURCE_BUSY: ReproductionState.WAITING_GLOBAL_RESOURCE,
        ReproductionEvent.CANCEL_REQUESTED: ReproductionState.CLEANUP,
        ReproductionEvent.RESOURCE_AVAILABLE: ReproductionState.AUTO_ARMING,
    },
    ReproductionState.WAITING_DEVICE_RESOURCE: {
        ReproductionEvent.RESOURCE_AVAILABLE: ReproductionState.AUTO_ARMING,
        ReproductionEvent.CANCEL_REQUESTED: ReproductionState.CLEANUP,
    },
    ReproductionState.WAITING_GLOBAL_RESOURCE: {
        ReproductionEvent.RESOURCE_AVAILABLE: ReproductionState.AUTO_ARMING,
        ReproductionEvent.CANCEL_REQUESTED: ReproductionState.CLEANUP,
    },
    ReproductionState.AUTO_ARMING: {
        ReproductionEvent.ARM_READY: ReproductionState.ARMED,
        ReproductionEvent.ARM_FAILED: ReproductionState.ARM_FAILED,
        ReproductionEvent.DEVICE_LOST: ReproductionState.DEVICE_LOST,
        ReproductionEvent.CANCEL_REQUESTED: ReproductionState.CLEANUP,
        ReproductionEvent.LEASE_EXPIRED: ReproductionState.ORPHANED,
    },
    ReproductionState.ARMED: {
        ReproductionEvent.WATCH_STARTED: ReproductionState.WATCHING,
        ReproductionEvent.DEVICE_LOST: ReproductionState.DEVICE_LOST,
        ReproductionEvent.CANCEL_REQUESTED: ReproductionState.CLEANUP,
        ReproductionEvent.LEASE_EXPIRED: ReproductionState.ORPHANED,
    },
    ReproductionState.WATCHING: {
        ReproductionEvent.ACTIVITY: ReproductionState.ACTIVITY_DETECTED,
        ReproductionEvent.CALL_BOUND: ReproductionState.CALL_DETECTED,
        ReproductionEvent.WATCH_TIMEOUT: ReproductionState.WATCH_TIMEOUT,
        ReproductionEvent.DEVICE_LOST: ReproductionState.DEVICE_LOST,
        ReproductionEvent.ENHANCEMENT_STARTED: ReproductionState.ENHANCING,
        ReproductionEvent.CAPTURE_RECOVERY_STARTED: ReproductionState.ENHANCING,
        ReproductionEvent.CLEANUP_STARTED: ReproductionState.CLEANUP,  # bounded experiment/session completion
        ReproductionEvent.CANCEL_REQUESTED: ReproductionState.CLEANUP,
        ReproductionEvent.LEASE_EXPIRED: ReproductionState.ORPHANED,
    },
    ReproductionState.ACTIVITY_DETECTED: {
        ReproductionEvent.CALL_BOUND: ReproductionState.CALL_DETECTED,
        ReproductionEvent.WATCH_STARTED: ReproductionState.WATCHING,  # invalid/no-call attempt
        ReproductionEvent.WATCH_TIMEOUT: ReproductionState.WATCH_TIMEOUT,
        ReproductionEvent.DEVICE_LOST: ReproductionState.DEVICE_LOST,
        ReproductionEvent.CLEANUP_STARTED: ReproductionState.CLEANUP,
        ReproductionEvent.CANCEL_REQUESTED: ReproductionState.CLEANUP,
        ReproductionEvent.LEASE_EXPIRED: ReproductionState.ORPHANED,
    },
    ReproductionState.CALL_DETECTED: {
        ReproductionEvent.CAPTURE_STARTED: ReproductionState.CAPTURING,
        ReproductionEvent.CALL_ENDED: ReproductionState.CALL_END_DETECTED,
        ReproductionEvent.DEVICE_LOST: ReproductionState.DEVICE_LOST,
        ReproductionEvent.CANCEL_REQUESTED: ReproductionState.CLEANUP,
        ReproductionEvent.LEASE_EXPIRED: ReproductionState.ORPHANED,
    },
    ReproductionState.CAPTURING: {
        ReproductionEvent.CALL_ENDED: ReproductionState.CALL_END_DETECTED,
        ReproductionEvent.CAPTURE_TIMEOUT: ReproductionState.CAPTURE_TIMEOUT,
        ReproductionEvent.DEVICE_LOST: ReproductionState.DEVICE_LOST,
        ReproductionEvent.CANCEL_REQUESTED: ReproductionState.CLEANUP,
        ReproductionEvent.LEASE_EXPIRED: ReproductionState.ORPHANED,
    },
    ReproductionState.CALL_END_DETECTED: {
        ReproductionEvent.POST_CAPTURE_STARTED: ReproductionState.POST_CAPTURE,
        ReproductionEvent.CANCEL_REQUESTED: ReproductionState.CLEANUP,
        ReproductionEvent.LEASE_EXPIRED: ReproductionState.ORPHANED,
    },
    ReproductionState.POST_CAPTURE: {
        # A session may continue watching after a non-target/insufficient call.
        ReproductionEvent.WATCH_STARTED: ReproductionState.WATCHING,
        ReproductionEvent.ENHANCEMENT_STARTED: ReproductionState.ENHANCING,
        ReproductionEvent.CAPTURE_RECOVERY_STARTED: ReproductionState.ENHANCING,
        ReproductionEvent.CLEANUP_STARTED: ReproductionState.CLEANUP,
        ReproductionEvent.CANCEL_REQUESTED: ReproductionState.CLEANUP,
        ReproductionEvent.LEASE_EXPIRED: ReproductionState.ORPHANED,
    },
    ReproductionState.ENHANCING: {
        ReproductionEvent.ENHANCEMENT_ARMED: ReproductionState.ARMED,
        ReproductionEvent.ARM_FAILED: ReproductionState.ARM_FAILED,
        ReproductionEvent.DEVICE_LOST: ReproductionState.DEVICE_LOST,
        ReproductionEvent.CANCEL_REQUESTED: ReproductionState.CLEANUP,
        ReproductionEvent.LEASE_EXPIRED: ReproductionState.ORPHANED,
    },
    ReproductionState.WAITING_EXTERNAL_ACTION: {
        # This state is reached only after cleanup in full workflow; Phase C mock keeps it explicit.
        ReproductionEvent.RESOURCE_AVAILABLE: ReproductionState.AUTO_ARMING,
        ReproductionEvent.CANCEL_REQUESTED: ReproductionState.CLEANUP,
    },
    ReproductionState.WATCH_TIMEOUT: {
        ReproductionEvent.CLEANUP_STARTED: ReproductionState.CLEANUP,
    },
    ReproductionState.CAPTURE_TIMEOUT: {
        ReproductionEvent.CLEANUP_STARTED: ReproductionState.CLEANUP,
    },
    ReproductionState.ARM_FAILED: {
        ReproductionEvent.CLEANUP_STARTED: ReproductionState.CLEANUP,
    },
    ReproductionState.DEVICE_LOST: {
        ReproductionEvent.CLEANUP_STARTED: ReproductionState.CLEANUP,
        ReproductionEvent.LEASE_EXPIRED: ReproductionState.ORPHANED,
    },
    ReproductionState.ORPHANED: {
        ReproductionEvent.CLEANUP_STARTED: ReproductionState.CLEANUP,
    },
    ReproductionState.CLEANUP: {
        ReproductionEvent.CLEANUP_VERIFIED: ReproductionState.FINALIZING,
        ReproductionEvent.CLEANUP_VERIFIED_EXTERNAL_WAIT: ReproductionState.WAITING_EXTERNAL_ACTION,
        ReproductionEvent.CLEANUP_DEGRADED: ReproductionState.CLEANUP_DEGRADED,
        ReproductionEvent.CLEANUP_FAILED: ReproductionState.CLEANUP_FAILED,
        ReproductionEvent.LEASE_EXPIRED: ReproductionState.ORPHANED,
    },
    ReproductionState.CLEANUP_DEGRADED: {
        ReproductionEvent.FINALIZE_STARTED: ReproductionState.FINALIZING,
        ReproductionEvent.CLEANUP_STARTED: ReproductionState.CLEANUP,
    },
    ReproductionState.CLEANUP_FAILED: {
        ReproductionEvent.CLEANUP_STARTED: ReproductionState.CLEANUP,
        ReproductionEvent.FINALIZE_STARTED: ReproductionState.FINALIZING,
    },
    ReproductionState.FINALIZING: {
        ReproductionEvent.ANALYSIS_STARTED: ReproductionState.ANALYZING,
        ReproductionEvent.CANCELLED: ReproductionState.CANCELLED,
        ReproductionEvent.SESSION_FAILED: ReproductionState.FAILED,
    },
    ReproductionState.ANALYZING: {
        ReproductionEvent.SESSION_COMPLETED: ReproductionState.COMPLETED,
        ReproductionEvent.SESSION_PARTIAL: ReproductionState.PARTIAL_SUCCESS,
        ReproductionEvent.SESSION_FAILED: ReproductionState.FAILED,
        ReproductionEvent.CANCELLED: ReproductionState.CANCELLED,
    },
}

TERMINAL_STATES = {
    ReproductionState.COMPLETED,
    ReproductionState.PARTIAL_SUCCESS,
    ReproductionState.CANCELLED,
    ReproductionState.FAILED,
}

LOCK_HOLDING_STATES = {
    ReproductionState.AUTO_ARMING,
    ReproductionState.ARMED,
    ReproductionState.WATCHING,
    ReproductionState.ACTIVITY_DETECTED,
    ReproductionState.CALL_DETECTED,
    ReproductionState.CAPTURING,
    ReproductionState.CALL_END_DETECTED,
    ReproductionState.POST_CAPTURE,
    ReproductionState.ENHANCING,
    ReproductionState.CLEANUP,
    ReproductionState.CLEANUP_DEGRADED,
    ReproductionState.CLEANUP_FAILED,
    ReproductionState.FINALIZING,
}


@dataclass(frozen=True)
class TransitionResult:
    previous: ReproductionState
    current: ReproductionState
    event: ReproductionEvent


def next_state(current: ReproductionState | str, event: ReproductionEvent | str) -> ReproductionState:
    current=ReproductionState(current)
    event=ReproductionEvent(event)
    try:
        return _TRANSITIONS[current][event]
    except KeyError as exc:
        raise AppError(
            'REPRODUCTION_TRANSITION_NOT_ALLOWED',
            details={'current_state':current.value,'event':event.value},
        ) from exc


def transition_session(
    db: Session,
    session: ReproductionSession,
    event: ReproductionEvent | str,
    *,
    actor: str | None = None,
    reason: str | None = None,
    payload: dict | None = None,
    source: str = 'SYSTEM',
) -> TransitionResult:
    event=ReproductionEvent(event)
    previous=ReproductionState(session.state)
    target=next_state(previous,event)
    now=_utcnow()
    before={'state':previous.value}
    session.state=target.value
    if target == ReproductionState.AUTO_ARMING and session.started_at is None:
        session.started_at=now
    if target in TERMINAL_STATES:
        session.ended_at=now
    db.add(ReproductionEventRecord(
        session_id=session.id,
        case_id=session.case_id,
        event_type=event.value,
        source=source,
        payload_json={'reason':reason, **(payload or {})},
    ))
    db.flush()
    audit(
        db,
        case_id=session.case_id,
        actor=actor,
        event_type=EventType.REPRODUCTION_STATE_CHANGED.value,
        action='REPRODUCTION_TRANSITION',
        target_type='reproduction_session',
        target_id=session.id,
        before=before,
        after={'state':target.value},
        reason=reason,
        detail={'previous_state':previous.value,'state':target.value,'event':event.value, **(payload or {})},
    )
    return TransitionResult(previous,target,event)
