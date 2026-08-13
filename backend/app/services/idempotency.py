from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.contracts.enums import IdempotencyStatus
from app.core.config import settings
from app.core.errors import AppError
from app.db.models import IdempotencyRecord


def _canonical_hash(payload: Any) -> str:
    encoded=json.dumps(payload,sort_keys=True,separators=(',',':'),ensure_ascii=False,default=str).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        # SQLite used by unit tests may round-trip timestamptz as naive. The
        # contract is UTC, so a missing tzinfo is interpreted as UTC.
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass
class IdempotencyHandle:
    record: IdempotencyRecord | None
    replay: dict | None = None
    replay_status: int | None = None


def begin_idempotent(db:Session, *, scope:str, key:str|None, payload:Any) -> IdempotencyHandle:
    if not key:
        return IdempotencyHandle(record=None)
    request_hash=_canonical_hash(payload)
    existing=db.scalar(select(IdempotencyRecord).where(IdempotencyRecord.scope==scope,IdempotencyRecord.idempotency_key==key))
    now=datetime.now(timezone.utc)
    if existing:
        if existing.request_hash!=request_hash:
            raise AppError('IDEMPOTENCY_KEY_REUSED',details={'scope':scope})
        if existing.status==IdempotencyStatus.COMPLETED.value and existing.response_json is not None:
            return IdempotencyHandle(existing,replay=existing.response_json,replay_status=existing.response_status)
        expires_at=_as_utc(existing.expires_at)
        expired=bool(expires_at and expires_at <= now)
        if existing.status==IdempotencyStatus.FAILED.value or expired:
            # Same request may be retried after an explicit failure or an abandoned/expired lease.
            existing.status=IdempotencyStatus.IN_PROGRESS.value
            existing.response_status=None; existing.response_json=None
            existing.resource_type=None; existing.resource_id=None
            existing.updated_at=now; existing.expires_at=now+timedelta(hours=settings.idempotency_ttl_hours)
            db.flush()
            return IdempotencyHandle(existing)
        raise AppError('IDEMPOTENCY_IN_PROGRESS',details={'scope':scope})
    row=IdempotencyRecord(scope=scope,idempotency_key=key,request_hash=request_hash,status=IdempotencyStatus.IN_PROGRESS.value,
                          expires_at=now+timedelta(hours=settings.idempotency_ttl_hours))
    db.add(row); db.flush()
    return IdempotencyHandle(record=row)


def complete_idempotent(db:Session, handle:IdempotencyHandle, *, response:dict, status_code:int, resource_type:str|None=None, resource_id:str|None=None):
    if not handle.record:
        return
    handle.record.status=IdempotencyStatus.COMPLETED.value
    handle.record.response_json=response
    handle.record.response_status=status_code
    handle.record.resource_type=resource_type
    handle.record.resource_id=resource_id
    handle.record.updated_at=datetime.now(timezone.utc)
    db.flush()


def fail_idempotent(db:Session, handle:IdempotencyHandle):
    if handle.record:
        handle.record.status=IdempotencyStatus.FAILED.value
        handle.record.updated_at=datetime.now(timezone.utc)
        db.flush()
