from __future__ import annotations

from sqlalchemy import event
from sqlalchemy.orm import Session

_INSTALLED_FACTORIES: set[int] = set()


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


def _refresh_after_commit(session: Session, session_factory):
    """Materialize Golden state only after the business transaction is durable."""
    if session.info.get("golden_candidate_refreshing"):
        return
    case_ids = set(session.info.pop("golden_candidate_case_ids", set()))
    if not case_ids:
        return

    from app.db.models import Case
    from app.golden.service import GoldenCandidateService

    db = session_factory()
    db.info["golden_candidate_refreshing"] = True
    try:
        service = GoldenCandidateService()
        for case_id in sorted(case_ids):
            if db.get(Case, case_id) is not None:
                service.refresh(db, case_id)
        db.commit()
    except Exception:
        db.rollback()
        # Sidecar failure never changes the already-committed Case result.  A later
        # Case change, explicit refresh, backfill or Eval export repairs the state.
    finally:
        db.close()


def _after_rollback(session: Session):
    session.info.pop("golden_candidate_case_ids", None)


def install_golden_candidate_session_hooks(session_factory) -> None:
    """Bind hooks only to the application's SessionLocal factory.

    This deliberately avoids global Session-class listeners so isolated test/tool
    sessions do not unexpectedly open the production database after commit.
    """
    key = id(session_factory)
    if key in _INSTALLED_FACTORIES:
        return
    event.listen(session_factory, "before_flush", _before_flush)
    event.listen(
        session_factory,
        "after_commit",
        lambda session: _refresh_after_commit(session, session_factory),
    )
    event.listen(session_factory, "after_rollback", _after_rollback)
    _INSTALLED_FACTORIES.add(key)
