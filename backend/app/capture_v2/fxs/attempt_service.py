from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select

from app.capture_v2.db_models import CaptureAttempt, CaptureEvent
from app.capture_v2.enums import AttemptClassification, CaptureAttemptState
from app.capture_v2.errors import CaptureV2Error
from app.capture_v2.fxs.sanitizer import SemanticActionType, SemanticFxsAction
from app.capture_v2.timeline.source_time import normalize_utc
from app.core.ids import new_id


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AttemptSemanticRepository:
    """Persist semantic Attempt state while CaptureEvent keeps raw/derived audit."""

    def __init__(self, session_factory):
        self.session_factory = session_factory

    def append_raw_event(self, *, capture_session_id: str, source_ts: datetime,
                         event: str, digit: str | None = None, line: int = 0) -> str:
        event_type = f"FXS_RAW_{event.upper()}"
        payload = {"digit": digit, "line": line}
        with self.session_factory() as db:
            candidates = list(db.scalars(select(CaptureEvent).where(
                CaptureEvent.capture_session_id == capture_session_id,
                CaptureEvent.entity_type == "FXS_RAW",
                CaptureEvent.event_type == event_type,
                CaptureEvent.source_ts == source_ts,
            )))
            for existing in candidates:
                if dict(existing.payload or {}) == payload:
                    return existing.id
        with self.session_factory() as db:
            with db.begin():
                row = CaptureEvent(
                    id=new_id(), capture_session_id=capture_session_id,
                    entity_type="FXS_RAW", entity_id=None,
                    event_type=event_type, source_ts=source_ts,
                    payload=payload,
                )
                db.add(row)
                db.flush()
                return row.id

    def _active(self, db, capture_session_id: str) -> CaptureAttempt | None:
        return db.scalar(select(CaptureAttempt).where(
            CaptureAttempt.capture_session_id == capture_session_id,
            CaptureAttempt.state.in_((
                CaptureAttemptState.PROVISIONAL.value,
                CaptureAttemptState.CONFIRMED.value,
                CaptureAttemptState.DATA_PLANE_VERIFYING.value,
            )),
        ).order_by(CaptureAttempt.attempt_no.desc()).limit(1))

    def apply(self, *, capture_session_id: str, action: SemanticFxsAction) -> str | None:
        with self.session_factory() as db:
            with db.begin():
                if action.action == SemanticActionType.PROVISIONAL_ATTEMPT:
                    active = self._active(db, capture_session_id)
                    if active is not None:
                        # A SIP/RTP fallback Attempt may already exist before a late
                        # raw OFFHOOK is delivered by the observer. Refine the
                        # candidate anchor using Source Time instead of creating a
                        # duplicate Attempt or ignoring the stronger FXS anchor.
                        if (
                            active.classification == AttemptClassification.FALLBACK_ANCHORED.value
                            and active.candidate_start_source_ts is not None
                            and normalize_utc(action.source_ts) < normalize_utc(active.candidate_start_source_ts)
                        ):
                            meta = dict(active.metadata_json or {})
                            history = list(meta.get("anchor_revision_history") or [])
                            history.append({
                                "from": active.candidate_start_source_ts.isoformat(),
                                "to": action.source_ts.isoformat(),
                                "source": "FXS_OFFHOOK",
                            })
                            meta["anchor_revision_history"] = history
                            active.candidate_start_source_ts = action.source_ts
                            active.confirmed_start_source_ts = action.source_ts
                            active.metadata_json = meta
                            db.add(CaptureEvent(
                                id=new_id(), capture_session_id=capture_session_id,
                                entity_type="CAPTURE_ATTEMPT", entity_id=active.id,
                                event_type="ATTEMPT_ANCHOR_REFINED", source_ts=action.source_ts,
                                payload=history[-1],
                            ))
                        return active.id
                    max_no = db.scalar(select(func.max(CaptureAttempt.attempt_no)).where(
                        CaptureAttempt.capture_session_id == capture_session_id
                    )) or 0
                    row = CaptureAttempt(
                        id=new_id(), capture_session_id=capture_session_id,
                        attempt_no=int(max_no) + 1, state=CaptureAttemptState.PROVISIONAL.value,
                        candidate_start_source_ts=action.source_ts,
                        metadata_json=dict(action.details),
                    )
                    db.add(row)
                    db.add(CaptureEvent(
                        id=new_id(), capture_session_id=capture_session_id,
                        entity_type="CAPTURE_ATTEMPT", entity_id=row.id,
                        event_type="ATTEMPT_PROVISIONAL", source_ts=action.source_ts,
                        payload=dict(action.details),
                    ))
                    return row.id

                current = self._active(db, capture_session_id)
                if action.action == SemanticActionType.CONFIRMED_ATTEMPT:
                    if current is None:
                        raise CaptureV2Error("PROVISIONAL_ATTEMPT_NOT_FOUND")
                    event_type = "ATTEMPT_CONFIRMED"
                    if current.state == CaptureAttemptState.PROVISIONAL.value:
                        current.state = CaptureAttemptState.CONFIRMED.value
                        current.confirmed_start_source_ts = current.candidate_start_source_ts
                        current.confirmation_source = action.details.get("confirmation_source")
                    elif current.classification == AttemptClassification.FALLBACK_ANCHORED.value:
                        # A signaling/RTP fallback may have confirmed the Attempt
                        # before the FXS observer delivers its embedded source-time
                        # OFFHOOK.  The semantic confirmation is corroboration, not
                        # a second Attempt or a duplicate state transition.
                        meta = dict(current.metadata_json or {})
                        if meta.get("fxs_corroborated") is True:
                            return current.id
                        meta["fxs_corroborated"] = True
                        meta["fxs_confirmation_source"] = action.details.get("confirmation_source")
                        meta["fxs_confirmed_at"] = normalize_utc(action.source_ts).isoformat()
                        current.metadata_json = meta
                        event_type = "ATTEMPT_FXS_CORROBORATED"
                    db.add(CaptureEvent(
                        id=new_id(), capture_session_id=capture_session_id,
                        entity_type="CAPTURE_ATTEMPT", entity_id=current.id,
                        event_type=event_type, source_ts=action.source_ts,
                        payload=dict(action.details),
                    ))
                    return current.id

                if action.action == SemanticActionType.FXS_HOOK_GLITCH:
                    if current is None:
                        return None
                    current.state = CaptureAttemptState.CLASSIFIED_GLITCH.value
                    current.ended_source_ts = action.source_ts
                    current.classification = AttemptClassification.FXS_HOOK_GLITCH.value
                    current.metadata_json = {**(current.metadata_json or {}), **action.details}
                    db.add(CaptureEvent(
                        id=new_id(), capture_session_id=capture_session_id,
                        entity_type="CAPTURE_ATTEMPT", entity_id=current.id,
                        event_type="FXS_HOOK_GLITCH", source_ts=action.source_ts,
                        payload=dict(action.details),
                    ))
                    return current.id

                if action.action == SemanticActionType.ATTEMPT_ENDED:
                    if current is None:
                        return None
                    if current.state == CaptureAttemptState.PROVISIONAL.value:
                        current.state = CaptureAttemptState.CONFIRMED.value
                        current.confirmed_start_source_ts = current.candidate_start_source_ts
                        current.confirmation_source = "END_EDGE"
                    current.state = CaptureAttemptState.ENDED.value
                    current.ended_source_ts = action.source_ts
                    current.classification = current.classification or AttemptClassification.NORMAL.value
                    db.add(CaptureEvent(
                        id=new_id(), capture_session_id=capture_session_id,
                        entity_type="CAPTURE_ATTEMPT", entity_id=current.id,
                        event_type="ATTEMPT_ENDED", source_ts=action.source_ts,
                        payload=dict(action.details),
                    ))
                    return current.id

                if action.action == SemanticActionType.FXS_HOOK_FLASH:
                    if current is not None:
                        db.add(CaptureEvent(
                            id=new_id(), capture_session_id=capture_session_id,
                            entity_type="CAPTURE_ATTEMPT", entity_id=current.id,
                            event_type="FXS_HOOK_FLASH", source_ts=action.source_ts,
                            payload=dict(action.details),
                        ))
                        return current.id
                    return None

                if action.action == SemanticActionType.DTMF:
                    if current is not None:
                        db.add(CaptureEvent(
                            id=new_id(), capture_session_id=capture_session_id,
                            entity_type="CAPTURE_ATTEMPT", entity_id=current.id,
                            event_type="FXS_DTMF", source_ts=action.source_ts,
                            payload=dict(action.details),
                        ))
                        return current.id
                return current.id if current is not None else None
