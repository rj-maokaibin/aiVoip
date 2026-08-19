from __future__ import annotations

from celery.signals import after_task_publish
from sqlalchemy import select

from app.db.ai_intelligence_models import AIDiagnosticCycle
from app.db.session import SessionLocal
from app.diagnosis.ai_suggest_bridge import AISuggestionBridge, AISuggestionBridgeError


_REPRODUCTION_TASK = "reproduction.start"


def _published_session_id(body) -> str | None:
    """Extract the first positional argument from Celery protocol v1/v2 bodies."""
    if isinstance(body, (list, tuple)) and body:
        args = body[0]
        if isinstance(args, (list, tuple)) and args:
            value = args[0]
            return str(value) if value else None
    if isinstance(body, dict):
        args = body.get("args")
        if isinstance(args, (list, tuple)) and args:
            value = args[0]
            return str(value) if value else None
    return None


def confirm_ai2_reproduction_publish(*, sender=None, headers=None, body=None, **kwargs) -> bool:
    """Mark the AI2 suggestion DISPATCHED only after broker publish succeeds.

    `after_task_publish` runs in the producer process after the message has been
    accepted by the broker. If publish raises, this function is not called and the
    Cycle remains ACCEPTED; a repeated card click then republishes the same persisted
    ReproductionSession instead of creating a second one.
    """
    task_name = str((headers or {}).get("task") or sender or "")
    if task_name != _REPRODUCTION_TASK:
        return False
    session_id = _published_session_id(body)
    if not session_id:
        return False

    with SessionLocal() as db:
        cycle = db.scalar(
            select(AIDiagnosticCycle)
            .where(
                AIDiagnosticCycle.execution_ref_type == "reproduction_session",
                AIDiagnosticCycle.execution_ref_id == session_id,
                AIDiagnosticCycle.suggestion_state == "ACCEPTED",
            )
            .order_by(AIDiagnosticCycle.created_at.desc())
            .limit(1)
            .with_for_update()
        )
        if cycle is None:
            return False
        try:
            AISuggestionBridge().mark_async_dispatched(
                db,
                case_id=cycle.case_id,
                cycle_id=cycle.id,
                execution_ref_id=session_id,
                actor="celery:after_task_publish",
            )
            db.commit()
        except AISuggestionBridgeError:
            db.rollback()
            return False
    return True


@after_task_publish.connect(weak=False)
def _after_task_publish(sender=None, headers=None, body=None, **kwargs):
    # Signal handlers must never make an already-successful broker publish appear
    # failed to the caller. Recovery remains possible because the worker/session is
    # deterministic and the reconciler can observe the persisted Session.
    try:
        confirm_ai2_reproduction_publish(sender=sender, headers=headers, body=body, **kwargs)
    except Exception:
        return None
    return None
