from __future__ import annotations

from sqlalchemy import event
from sqlalchemy.orm import Session

_INSTALLED = False


def _case_id(obj):
    # Any persisted Case-owned domain object may advance Golden eligibility.
    # Audit/assessment writes are excluded to prevent self-triggered loops.
    name = type(obj).__name__
    if name in {"AuditLog", "GoldenCandidateAssessment"}:
        return None
    value = getattr(obj, "case_id", None)
    if value:
        return str(value)
    if name == "Case":
        value = getattr(obj, "id", None)
        if value:
            return str(value)
    return None


def _before_flush(session: Session, flush_context, instances):
    if session.info.get("golden_candidate_refreshing"):
        return
    pending = session.info.setdefault("golden_candidate_case_ids", set())
    for obj in list(session.new) + list(session.dirty) + list(session.deleted):
        case_id = _case_id(obj)
        if case_id:
            pending.add(case_id)


def _after_commit(session: Session):
    """Materialize Golden state after the business transaction is durable.

    Assessment runs in a fresh transaction.  This avoids performing a nested flush
    from SQLAlchemy flush events and, importantly, means a failed business commit can
    never advance Golden state.  The refresh transaction is marked so its own writes
    do not recursively schedule another refresh.
    """
    if session.info.get("golden_candidate_refreshing"):
        return
    case_ids = set(session.info.pop("golden_candidate_case_ids", set()))
    if not case_ids:
        return

    from app.db.models import Case
    from app.db.session import SessionLocal
    from app.golden.service import GoldenCandidateService

    db = SessionLocal()
    db.info["golden_candidate_refreshing"] = True
    try:
        service = GoldenCandidateService()
        for case_id in sorted(case_ids):
            if db.get(Case, case_id) is not None:
                service.refresh(db, case_id)
        db.commit()
    except Exception:
        db.rollback()
        # Golden accumulation is an observability/quality sidecar.  It must never
        # turn an already-committed Case operation into a user-visible failure.
        # The next Case change, API refresh, backfill or Eval export repairs state.
    finally:
        db.close()


def _after_rollback(session: Session):
    session.info.pop("golden_candidate_case_ids", None)


def install_golden_candidate_session_hooks() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    event.listen(Session, "before_flush", _before_flush)
    event.listen(Session, "after_commit", _after_commit)
    event.listen(Session, "after_rollback", _after_rollback)
    _INSTALLED = True
