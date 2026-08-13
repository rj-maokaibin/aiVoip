from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from app.api.deps import require_roles
from app.contracts.enums import UserRole
from app.core.config import settings
from app.db.models import EventOutbox
from app.db.session import SessionLocal

router = APIRouter(tags=["events"])


def _encode(row: EventOutbox) -> str:
    envelope = {
        "event_id": row.event_id,
        "event_type": row.event_type,
        "schema_version": row.schema_version,
        "case_id": row.case_id,
        "entity_type": row.entity_type,
        "entity_id": row.entity_id,
        "timestamp": row.created_at.isoformat() if row.created_at else None,
        "payload": row.payload_json or {},
    }
    return f"id: {row.seq}\nevent: {row.event_type}\ndata: {json.dumps(envelope, ensure_ascii=False, separators=(',', ':'))}\n\n"


@router.get('/events/stream')
def stream_events(
    case_id: str | None = Query(default=None),
    after: int | None = Query(default=None, ge=0),
    last_event_id: str | None = Header(default=None, alias='Last-Event-ID'),
    _identity=Depends(require_roles(UserRole.VIEWER, UserRole.ENGINEER, UserRole.EXPERT_REVIEWER, UserRole.ADMIN, UserRole.SERVICE)),
):
    try:
        cursor = int(last_event_id) if last_event_id is not None else int(after or 0)
    except ValueError:
        cursor = int(after or 0)

    async def generate():
        nonlocal cursor
        while True:
            with SessionLocal() as db:
                stmt = select(EventOutbox).where(EventOutbox.seq > cursor)
                if case_id:
                    stmt = stmt.where(EventOutbox.case_id == case_id)
                rows = list(db.scalars(stmt.order_by(EventOutbox.seq.asc()).limit(settings.sse_batch_size)))
            if rows:
                for row in rows:
                    cursor = row.seq
                    yield _encode(row)
            else:
                yield ': keepalive\n\n'
            await asyncio.sleep(settings.sse_poll_interval_seconds)

    return StreamingResponse(generate(), media_type='text/event-stream', headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})
