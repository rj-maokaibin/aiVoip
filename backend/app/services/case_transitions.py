from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.contracts.enums import CaseEvent, CaseStatus, EvidenceCompleteness
from app.core.errors import AppError
from app.db.models import Case, CaseStateHistory, Evidence
from app.services.audit import audit

Guard = Callable[[dict[str, Any]], bool]
ADMIN_CLOSE_EVENT = "CASE_ADMIN_CLOSED"
_ADMIN_CLOSE_ACTOR_PREFIXES = ("github-admin:", "system-admin:")


@dataclass(frozen=True)
class TransitionRule:
    from_states: frozenset[CaseStatus]
    event: CaseEvent
    to_state: CaseStatus
    guard_name: str | None = None


TRANSITIONS: tuple[TransitionRule, ...] = (
    TransitionRule(frozenset({CaseStatus.NEW}), CaseEvent.TRIAGE_STARTED, CaseStatus.TRIAGING),
    TransitionRule(frozenset({CaseStatus.NEW, CaseStatus.TRIAGING, CaseStatus.NEED_MORE_EVIDENCE}), CaseEvent.COLLECTION_STARTED, CaseStatus.COLLECTING),
    TransitionRule(frozenset({CaseStatus.COLLECTING}), CaseEvent.COLLECTION_COMPLETED, CaseStatus.ANALYZING),
    TransitionRule(frozenset({CaseStatus.NEW, CaseStatus.TRIAGING, CaseStatus.COLLECTING, CaseStatus.NEED_MORE_EVIDENCE, CaseStatus.WAITING_USER, CaseStatus.DIAGNOSED}), CaseEvent.ANALYSIS_STARTED, CaseStatus.ANALYZING),
    TransitionRule(frozenset({CaseStatus.ANALYZING, CaseStatus.DIAGNOSED}), CaseEvent.EVIDENCE_REQUIRED, CaseStatus.NEED_MORE_EVIDENCE),
    TransitionRule(frozenset({CaseStatus.TRIAGING, CaseStatus.ANALYZING, CaseStatus.NEED_MORE_EVIDENCE, CaseStatus.DIAGNOSED}), CaseEvent.USER_ACTION_REQUIRED, CaseStatus.WAITING_USER),
    TransitionRule(frozenset({CaseStatus.ANALYZING}), CaseEvent.DIAGNOSIS_COMPLETED, CaseStatus.DIAGNOSED),
    TransitionRule(frozenset({CaseStatus.ANALYZING, CaseStatus.DIAGNOSED, CaseStatus.WAITING_USER, CaseStatus.NEED_MORE_EVIDENCE}), CaseEvent.ROOT_CAUSE_CONFIRMED, CaseStatus.ROOT_CAUSE_CONFIRMED),
    TransitionRule(frozenset({CaseStatus.ROOT_CAUSE_CONFIRMED}), CaseEvent.RESOLUTION_STARTED, CaseStatus.RESOLVING),
    TransitionRule(frozenset({CaseStatus.RESOLVING}), CaseEvent.FIX_VERIFIED, CaseStatus.RESOLVED, "fix_verification_evidence"),
    TransitionRule(frozenset({CaseStatus.RESOLVING}), CaseEvent.FIX_REEVALUATION_REQUIRED, CaseStatus.ANALYZING),
    TransitionRule(frozenset({CaseStatus.RESOLVED}), CaseEvent.CASE_CLOSED, CaseStatus.CLOSED),
)

_FAILABLE = frozenset({
    CaseStatus.NEW, CaseStatus.TRIAGING, CaseStatus.COLLECTING, CaseStatus.ANALYZING,
    CaseStatus.NEED_MORE_EVIDENCE, CaseStatus.WAITING_USER, CaseStatus.DIAGNOSED,
    CaseStatus.ROOT_CAUSE_CONFIRMED, CaseStatus.RESOLVING,
})
_ADMIN_CLOSABLE = _FAILABLE | frozenset({CaseStatus.RESOLVED})


def _guard_ok(db: Session, case: Case, name: str | None, context: dict[str, Any]) -> bool:
    if name is None:
        return True
    if name == "fix_verification_evidence":
        evidence_id = context.get("fix_verification_evidence_id")
        if not evidence_id:
            return False
        evidence = db.get(Evidence, evidence_id)
        if not evidence or evidence.case_id != case.id:
            return False
        result = str((evidence.metadata_json or {}).get("result", "")).upper()
        return evidence.completeness == EvidenceCompleteness.COMPLETE.value and result == "FIX_VERIFIED"
    return False


class CaseTransitionService:
    @staticmethod
    def transition(
        db: Session,
        case: Case,
        event: CaseEvent | str,
        *,
        reason: str,
        actor: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> Case:
        context = dict(context or {})
        event = CaseEvent(event)
        current = CaseStatus(case.status)

        if event == CaseEvent.CASE_FAILED:
            if current == CaseStatus.FAILED:
                return case
            if current not in _FAILABLE:
                raise AppError("CASE_TRANSITION_NOT_ALLOWED", details={"from": current.value, "event": event.value})
            target = CaseStatus.FAILED
        else:
            rule = next((x for x in TRANSITIONS if x.event == event and current in x.from_states), None)
            if not rule:
                candidates = [x for x in TRANSITIONS if x.event == event]
                if any(x.to_state == current for x in candidates):
                    return case
                raise AppError("CASE_TRANSITION_NOT_ALLOWED", details={"from": current.value, "event": event.value})
            if not _guard_ok(db, case, rule.guard_name, context):
                if rule.guard_name == "fix_verification_evidence":
                    raise AppError("FIX_VERIFICATION_EVIDENCE_REQUIRED")
                raise AppError("CASE_TRANSITION_NOT_ALLOWED", details={"guard": rule.guard_name})
            target = rule.to_state

        old = case.status
        case.status = target.value
        case.updated_at = datetime.now(timezone.utc)
        db.add(CaseStateHistory(case_id=case.id, from_status=old, to_status=target.value, event=event.value, actor=actor, reason=reason, context_json=context))
        audit(
            db,
            case_id=case.id,
            actor=actor,
            event_type="CASE_STATE_CHANGED",
            target_type="case",
            target_id=case.id,
            before={"status": old}, after={"status": target.value}, reason=reason, detail={"from": old, "to": target.value, "event": event.value, "reason": reason, "context": context},
        )
        db.flush()
        return case

    @staticmethod
    def administrative_close(
        db: Session,
        case: Case,
        *,
        reason: str,
        actor: str,
        context: dict[str, Any] | None = None,
    ) -> Case:
        """Close a stale/administratively retired Case without claiming a verified fix.

        This is deliberately separate from CASE_CLOSED (RESOLVED -> CLOSED). It
        never emits FIX_VERIFIED or RESOLVED. Only controlled administrator actors
        may use it, and FAILED is preserved as a distinct terminal outcome.
        """
        actor = str(actor or "").strip()
        reason = str(reason or "").strip()
        if not actor.startswith(_ADMIN_CLOSE_ACTOR_PREFIXES):
            raise AppError("CASE_ADMIN_CLOSE_FORBIDDEN")
        if not reason:
            raise AppError("CASE_ADMIN_CLOSE_REASON_REQUIRED")

        current = CaseStatus(case.status)
        if current == CaseStatus.CLOSED:
            return case
        if current == CaseStatus.FAILED:
            raise AppError("CASE_ADMIN_CLOSE_FAILED_CASE_FORBIDDEN")
        if current not in _ADMIN_CLOSABLE:
            raise AppError("CASE_TRANSITION_NOT_ALLOWED", details={"from": current.value, "event": ADMIN_CLOSE_EVENT})

        details = dict(context or {})
        details["administrative"] = True
        details["preserves_fix_semantics"] = True
        old = case.status
        case.status = CaseStatus.CLOSED.value
        case.updated_at = datetime.now(timezone.utc)
        db.add(CaseStateHistory(
            case_id=case.id,
            from_status=old,
            to_status=CaseStatus.CLOSED.value,
            event=ADMIN_CLOSE_EVENT,
            actor=actor,
            reason=reason,
            context_json=details,
        ))
        audit(
            db,
            case_id=case.id,
            actor=actor,
            event_type="CASE_STATE_CHANGED",
            target_type="case",
            target_id=case.id,
            before={"status": old},
            after={"status": CaseStatus.CLOSED.value},
            reason=reason,
            detail={
                "from": old,
                "to": CaseStatus.CLOSED.value,
                "event": ADMIN_CLOSE_EVENT,
                "reason": reason,
                "context": details,
            },
        )
        db.flush()
        return case
