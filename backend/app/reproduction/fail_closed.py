from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.contracts.enums import LockStatus, ReproductionEvent, ReproductionState
from app.db.models import DeviceDiagnosticLock, ReproductionEventRecord, ReproductionSession
from app.reproduction.locks import release_device_lock_forced
from app.reproduction.state_machine import TERMINAL_STATES, transition_session


def error_code(exc: BaseException) -> str:
    code = getattr(exc, "code", None)
    if code:
        return str(code)
    return type(exc).__name__


def error_details(exc: BaseException) -> dict[str, Any]:
    details = getattr(exc, "details", None)
    return dict(details) if isinstance(details, dict) else {}


def session_has_active_lock(db: Session, session_id: str) -> bool:
    return (
        db.scalar(
            select(DeviceDiagnosticLock.id).where(
                DeviceDiagnosticLock.session_id == session_id,
                DeviceDiagnosticLock.status == LockStatus.ACTIVE.value,
            )
        )
        is not None
    )


def session_has_any_progress_event(db: Session, session_id: str) -> bool:
    return (
        db.scalar(
            select(ReproductionEventRecord.id).where(
                ReproductionEventRecord.session_id == session_id
            )
        )
        is not None
    )


def fail_closed_startup(
    db: Session,
    *,
    session: ReproductionSession,
    error: BaseException,
    actor: str,
    ownership: bool = False,
) -> str:
    """Move a reproduction session out of a stuck non-terminal startup state.

    Fail-closed contract (never leave a session silently in CREATED):
      * If ``ownership`` is True (a committed DUT lock and/or Capture ownership
        already exists) the caller must dispatch formal cleanup/recovery instead;
        this helper returns ``NEEDS_CLEANUP`` and leaves the session untouched.
      * Otherwise the session is advanced CREATED -> AUTO_ARMING -> ARM_FAILED
        (or AUTO_ARMING/ENHANCING -> ARM_FAILED) with the failure recorded as
        terminal_reason + state events + audit, and any in-transaction lock is
        released.  Returns ``FAIL_CLOSED``.

    ``error`` may be any exception; the code/detail extraction is defensive.
    """
    code = error_code(error)
    details = error_details(error)
    state = ReproductionState(session.state)
    if state in TERMINAL_STATES:
        return "ALREADY_TERMINAL"
    if ownership:
        return "NEEDS_CLEANUP"
    if state == ReproductionState.CREATED:
        transition_session(
            db, session, ReproductionEvent.START_ARMING, actor=actor, reason="fail_closed_startup"
        )
        transition_session(
            db, session, ReproductionEvent.ARM_FAILED, actor=actor, reason=code, payload=details
        )
    elif state in {ReproductionState.AUTO_ARMING, ReproductionState.ENHANCING}:
        transition_session(
            db, session, ReproductionEvent.ARM_FAILED, actor=actor, reason=code, payload=details
        )
    session.terminal_reason = code
    session.terminal_detail_json = details
    # Nothing was started on the DUT, so no platform cleanup is required.
    session.cleanup_required = False
    session.cleanup_status = "NOT_REQUIRED"
    release_device_lock_forced(db, session=session)
    return "FAIL_CLOSED"
