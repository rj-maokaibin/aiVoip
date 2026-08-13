from __future__ import annotations

from app.contracts.enums import ActorType
from app.db.models import AuditLog
from app.services.events import emit_event


def audit(
    db,
    *,
    case_id=None,
    actor=None,
    actor_type: ActorType | str | None = None,
    event_type,
    action: str | None = None,
    target_type=None,
    target_id=None,
    before: dict | None = None,
    after: dict | None = None,
    reason: str | None = None,
    trace_id: str | None = None,
    detail=None,
):
    actor_type_value = actor_type.value if isinstance(actor_type, ActorType) else actor_type
    if actor_type_value is None:
        actor_type_value = ActorType.SYSTEM.value if actor is None else ActorType.USER.value
    row=AuditLog(
        case_id=case_id,
        actor=actor,
        actor_type=actor_type_value,
        event_type=str(event_type),
        action=action or str(event_type),
        target_type=target_type,
        target_id=target_id,
        before_json=before,
        after_json=after,
        reason=reason,
        trace_id=trace_id,
        detail=detail,
    )
    db.add(row); db.flush()
    emit_event(db, event_type=str(event_type), case_id=case_id, entity_type=target_type, entity_id=target_id, payload=detail or after or {})
    return row
