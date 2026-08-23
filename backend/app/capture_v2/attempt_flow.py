from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from app.capture_v2.db_models import CaptureAttempt, CaptureEvent
from app.capture_v2.enums import CaptureAttemptState
from app.capture_v2.errors import CaptureV2Error
from app.core.ids import new_id


def _utc(value: datetime | None = None) -> datetime:
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class CaptureAttemptFlow:
    def __init__(self, session_factory):
        self.session_factory = session_factory

    def active(self, capture_session_id: str) -> CaptureAttempt | None:
        with self.session_factory() as db:
            return db.scalar(select(CaptureAttempt).where(
                CaptureAttempt.capture_session_id == capture_session_id,
                CaptureAttempt.state.in_((
                    CaptureAttemptState.PROVISIONAL.value,
                    CaptureAttemptState.CONFIRMED.value,
                    CaptureAttemptState.DATA_PLANE_VERIFYING.value,
                )),
            ).order_by(CaptureAttempt.attempt_no.desc()).limit(1))

    def _set(self, capture_attempt_id: str, *, allowed: tuple[str, ...], target: str,
             source_ts: datetime | None = None, event_type: str, payload: dict | None = None) -> None:
        with self.session_factory() as db:
            with db.begin():
                row = db.get(CaptureAttempt, capture_attempt_id)
                if row is None:
                    raise CaptureV2Error("CAPTURE_ATTEMPT_NOT_FOUND")
                if row.state == target:
                    return
                if row.state not in allowed:
                    raise CaptureV2Error(
                        "CAPTURE_ATTEMPT_TRANSITION_INVALID",
                        details={"from": row.state, "to": target},
                    )
                row.state = target
                db.add(CaptureEvent(
                    id=new_id(), capture_session_id=row.capture_session_id,
                    entity_type="CAPTURE_ATTEMPT", entity_id=row.id,
                    event_type=event_type, source_ts=_utc(source_ts), payload=payload or {},
                ))

    def begin_data_plane(self, capture_attempt_id: str, *, source_ts: datetime | None = None) -> None:
        self._set(
            capture_attempt_id,
            allowed=(CaptureAttemptState.CONFIRMED.value,),
            target=CaptureAttemptState.DATA_PLANE_VERIFYING.value,
            source_ts=source_ts, event_type="ATTEMPT_DATA_PLANE_VERIFYING",
        )

    def begin_evidence_finalizing(self, capture_attempt_id: str, *, source_ts: datetime | None = None) -> None:
        self._set(
            capture_attempt_id,
            allowed=(CaptureAttemptState.ENDED.value,),
            target=CaptureAttemptState.EVIDENCE_FINALIZING.value,
            source_ts=source_ts, event_type="ATTEMPT_EVIDENCE_FINALIZING",
        )

    def mark_evaluated(self, capture_attempt_id: str, *, source_ts: datetime | None = None,
                       quality_snapshot_id: str | None = None) -> None:
        self._set(
            capture_attempt_id,
            allowed=(CaptureAttemptState.EVIDENCE_FINALIZING.value,),
            target=CaptureAttemptState.EVALUATED.value,
            source_ts=source_ts, event_type="ATTEMPT_EVALUATED",
            payload={"quality_snapshot_id": quality_snapshot_id},
        )
