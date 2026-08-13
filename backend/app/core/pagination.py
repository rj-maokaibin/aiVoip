from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.core.errors import AppError


def _b64encode(payload: dict[str, Any]) -> str:
    raw=json.dumps(payload,separators=(',',':'),sort_keys=True).encode('utf-8')
    return base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')


def _b64decode(token: str) -> dict[str, Any]:
    try:
        padded=token+'='*((4-len(token)%4)%4)
        return json.loads(base64.urlsafe_b64decode(padded.encode('ascii')).decode('utf-8'))
    except Exception as exc:
        raise AppError('INVALID_CURSOR') from exc


def encode_created_cursor(created_at: datetime, row_id: str) -> str:
    if created_at.tzinfo is None:
        created_at=created_at.replace(tzinfo=timezone.utc)
    return _b64encode({'created_at':created_at.astimezone(timezone.utc).isoformat(),'id':row_id})


def decode_created_cursor(token: str | None) -> tuple[datetime,str] | None:
    if not token:
        return None
    data=_b64decode(token)
    try:
        dt=datetime.fromisoformat(str(data['created_at']).replace('Z','+00:00'))
        if dt.tzinfo is None: dt=dt.replace(tzinfo=timezone.utc)
        return dt, str(data['id'])
    except Exception as exc:
        raise AppError('INVALID_CURSOR') from exc


def paginate_created(
    db: Session,
    model,
    *,
    where=(),
    limit: int = 50,
    cursor: str | None = None,
    descending: bool = False,
):
    limit=max(1,min(int(limit),200))
    stmt=select(model)
    for condition in where:
        stmt=stmt.where(condition)
    decoded=decode_created_cursor(cursor)
    if decoded:
        dt,row_id=decoded
        if descending:
            stmt=stmt.where(or_(model.created_at < dt, and_(model.created_at == dt, model.id < row_id)))
        else:
            stmt=stmt.where(or_(model.created_at > dt, and_(model.created_at == dt, model.id > row_id)))
    order=(model.created_at.desc(),model.id.desc()) if descending else (model.created_at.asc(),model.id.asc())
    rows=list(db.scalars(stmt.order_by(*order).limit(limit+1)))
    has_more=len(rows)>limit
    items=rows[:limit]
    next_cursor=encode_created_cursor(items[-1].created_at,items[-1].id) if has_more and items else None
    return items,next_cursor,has_more
