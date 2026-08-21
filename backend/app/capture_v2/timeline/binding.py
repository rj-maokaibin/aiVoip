from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select

from app.capture_v2.db_models import CaptureAttempt, CaptureEvent
from app.capture_v2.enums import AttemptClassification, CaptureAttemptState
from app.capture_v2.errors import CaptureV2Error
from app.capture_v2.timeline.source_time import normalize_utc
from app.core.ids import new_id



@dataclass(frozen=True)
class BindingResult:
    capture_attempt_id: str
    created_fallback: bool
    refined: bool
    binding_event: str
    call_ref: str | None


class EventualBindingService:
    """Source-time correlation independent of download/analysis time."""

    def __init__(self, session_factory):
        self.session_factory = session_factory

    def _candidate(self, db, capture_session_id: str, source_ts: datetime) -> CaptureAttempt | None:
        source_ts = normalize_utc(source_ts)
        rows = list(db.scalars(select(CaptureAttempt).where(
            CaptureAttempt.capture_session_id == capture_session_id,
            CaptureAttempt.state != CaptureAttemptState.CLASSIFIED_GLITCH.value,
        ).order_by(CaptureAttempt.attempt_no.desc())))
        for row in rows:
            start = row.confirmed_start_source_ts or row.candidate_start_source_ts
            if start is None:
                continue
            start = normalize_utc(start)
            end = normalize_utc(row.ended_source_ts) if row.ended_source_ts is not None else None
            if start <= source_ts and (end is None or source_ts <= end):
                return row
        return None

    def bind(self, *, capture_session_id: str, source_ts: datetime,
             binding_event: str, call_ref: str | None = None,
             allow_fallback_create: bool = True, details: dict | None = None) -> BindingResult:
        source_ts = normalize_utc(source_ts)
        with self.session_factory() as db:
            with db.begin():
                attempt = self._candidate(db, capture_session_id, source_ts)
                created = False
                if attempt is None:
                    if not allow_fallback_create:
                        raise CaptureV2Error(
                            "CALL_BINDING_ATTEMPT_NOT_FOUND",
                            details={"binding_event": binding_event},
                        )
                    max_no = max((r.attempt_no for r in db.scalars(select(CaptureAttempt).where(
                        CaptureAttempt.capture_session_id == capture_session_id
                    ))), default=0)
                    attempt = CaptureAttempt(
                        id=new_id(), capture_session_id=capture_session_id,
                        attempt_no=int(max_no) + 1,
                        state=CaptureAttemptState.CONFIRMED.value,
                        candidate_start_source_ts=source_ts,
                        confirmed_start_source_ts=source_ts,
                        confirmation_source=binding_event,
                        classification=AttemptClassification.FALLBACK_ANCHORED.value,
                        metadata_json={"anchor_revision_history": []},
                    )
                    db.add(attempt)
                    db.flush()
                    created = True
                    db.add(CaptureEvent(
                        id=new_id(), capture_session_id=capture_session_id,
                        entity_type="CAPTURE_ATTEMPT", entity_id=attempt.id,
                        event_type="ATTEMPT_FALLBACK_CREATED", source_ts=source_ts,
                        payload={"binding_event": binding_event, "call_ref": call_ref},
                    ))

                if attempt.state == CaptureAttemptState.PROVISIONAL.value:
                    attempt.state = CaptureAttemptState.CONFIRMED.value
                    attempt.confirmed_start_source_ts = attempt.candidate_start_source_ts
                    attempt.confirmation_source = f"CALL_{state}"
                    db.add(CaptureEvent(
                        id=new_id(), capture_session_id=attempt.capture_session_id,
                        entity_type="CAPTURE_ATTEMPT", entity_id=attempt.id,
                        event_type="ATTEMPT_CONFIRMED", source_ts=normalize_utc(source_ts),
                        payload={"confirmation_source": f"CALL_{state}"},
                    ))

                meta = dict(attempt.metadata_json or {})
                call = dict(meta.get("call") or {})
                existing_ref = call.get("call_ref")
                if existing_ref and call_ref and existing_ref != call_ref:
                    raise CaptureV2Error(
                        "CALL_BINDING_CONFLICT",
                        details={"existing_call_ref": existing_ref, "new_call_ref": call_ref},
                    )
                call.update({
                    "call_ref": existing_ref or call_ref,
                    "binding_event": call.get("binding_event") or binding_event,
                    "binding_source_ts": call.get("binding_source_ts") or source_ts.isoformat(),
                    "state": call.get("state") or "CONFIRMED",
                })
                call.setdefault("history", []).append({
                    "event": binding_event,
                    "source_ts": source_ts.isoformat(),
                    **(details or {}),
                })
                meta["call"] = call
                attempt.metadata_json = meta
                db.add(CaptureEvent(
                    id=new_id(), capture_session_id=capture_session_id,
                    entity_type="CAPTURE_ATTEMPT", entity_id=attempt.id,
                    event_type="CALL_BOUND", source_ts=source_ts,
                    payload={"binding_event": binding_event, "call_ref": call.get("call_ref"), **(details or {})},
                ))
                return BindingResult(attempt.id, created, False, binding_event, call.get("call_ref"))

    def update_call_state(self, *, capture_attempt_id: str, state: str,
                          source_ts: datetime, details: dict | None = None) -> None:
        with self.session_factory() as db:
            with db.begin():
                attempt = db.get(CaptureAttempt, capture_attempt_id)
                if attempt is None:
                    raise CaptureV2Error("CAPTURE_ATTEMPT_NOT_FOUND")
                if attempt.state == CaptureAttemptState.PROVISIONAL.value:
                    attempt.state = CaptureAttemptState.CONFIRMED.value
                    attempt.confirmed_start_source_ts = attempt.candidate_start_source_ts
                    attempt.confirmation_source = f"CALL_{state}"
                    db.add(CaptureEvent(
                        id=new_id(), capture_session_id=attempt.capture_session_id,
                        entity_type="CAPTURE_ATTEMPT", entity_id=attempt.id,
                        event_type="ATTEMPT_CONFIRMED", source_ts=normalize_utc(source_ts),
                        payload={"confirmation_source": f"CALL_{state}"},
                    ))

                meta = dict(attempt.metadata_json or {})
                call = dict(meta.get("call") or {})
                if not call:
                    raise CaptureV2Error("CAPTURE_CALL_NOT_BOUND")
                if call.get("state") == state:
                    return
                call["state"] = state
                call.setdefault("history", []).append({
                    "state": state, "source_ts": normalize_utc(source_ts).isoformat(), **(details or {})
                })
                meta["call"] = call
                attempt.metadata_json = meta
                db.add(CaptureEvent(
                    id=new_id(), capture_session_id=attempt.capture_session_id,
                    entity_type="CAPTURE_ATTEMPT", entity_id=attempt.id,
                    event_type=f"CALL_{state}", source_ts=normalize_utc(source_ts), payload=details or {},
                ))
