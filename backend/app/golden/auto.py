from __future__ import annotations

from sqlalchemy import event
from sqlalchemy.orm import Session

_INSTALLED = False


def _case_id(obj):
    # Avoid importing all domain models here; the hook intentionally works for any
    # Case-owned persisted object that exposes case_id.  Audit/assessment writes are
    # excluded to prevent self-triggered loops.
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


def _after_flush_postexec(session: Session, flush_context):
    if session.info.get("golden_candidate_refreshing"):
        return
    case_ids = set(session.info.pop("golden_candidate_case_ids", set()))
    if not case_ids:
        return
    session.info["golden_candidate_refreshing"] = True
    try:
        from app.db.models import Case
        from app.golden.service import GoldenCandidateService

        service = GoldenCandidateService()
        for case_id in sorted(case_ids):
            # A Case may have been deleted in the same transaction.
            if session.get(Case, case_id) is not None:
                service.refresh(session, case_id)
        # Do not call session.flush() from a flush event.  During Session.commit(),
        # SQLAlchemy automatically performs another flush when new/dirty assessment
        # rows were created here.  The guard/exclusions keep that second flush finite.
    finally:
        session.info["golden_candidate_refreshing"] = False


def install_golden_candidate_session_hooks() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    event.listen(Session, "before_flush", _before_flush)
    event.listen(Session, "after_flush_postexec", _after_flush_postexec)
    _INSTALLED = True
