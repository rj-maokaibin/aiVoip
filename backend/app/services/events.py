from __future__ import annotations

from sqlalchemy.orm import Session

from app.contracts.enums import EventType
from app.db.models import EventOutbox

EVENT_REGISTRY = {x.value for x in EventType}


def emit_event(
    db: Session,
    *,
    event_type: EventType | str,
    case_id: str | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    payload: dict | None = None,
    allow_unregistered: bool = False,
) -> EventOutbox | None:
    raw = event_type.value if isinstance(event_type, EventType) else str(event_type)
    if raw not in EVENT_REGISTRY and not allow_unregistered:
        return None
    row = EventOutbox(
        event_type=raw,
        case_id=case_id,
        entity_type=entity_type,
        entity_id=entity_id,
        payload_json=payload or {},
    )
    db.add(row)
    db.flush()
    return row
