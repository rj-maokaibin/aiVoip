from datetime import datetime
from pydantic import BaseModel, ConfigDict

from app.contracts.enums import ActorType


class AuditLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    case_id: str | None = None
    actor: str | None = None
    actor_type: ActorType | None = None
    event_type: str
    action: str | None = None
    target_type: str | None = None
    target_id: str | None = None
    before_json: dict | None = None
    after_json: dict | None = None
    reason: str | None = None
    trace_id: str | None = None
    detail: dict | None = None
    created_at: datetime
