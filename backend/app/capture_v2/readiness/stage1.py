from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from app.capture_v2.db_models import CaptureEvent, CaptureSession, ReadinessSnapshot
from app.capture_v2.enums import CaptureSessionState, ReadinessStage, ReadinessStatus
from app.capture_v2.errors import CaptureV2Error
from app.core.ids import new_id


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class CapturePathChecks:
    lease_active: bool
    exactly_one_producer: bool
    voice_context_ready: bool
    pcap_ready: bool
    fxs_ready: bool
    pcm_control_ready: bool
    server_store_ready: bool
    transfer_ready: bool
    storage_guard_ready: bool
    watchdog_ready: bool

    def as_dict(self) -> dict[str, bool]:
        return {name: bool(value) for name, value in self.__dict__.items()}


@dataclass(frozen=True)
class ReadinessDecision:
    status: ReadinessStatus
    reasons: tuple[str, ...]
    checks: dict[str, bool]


class CapturePathReadinessEvaluator:
    @staticmethod
    def evaluate(checks: CapturePathChecks) -> ReadinessDecision:
        values = checks.as_dict()
        reasons = tuple(f"{name.upper()}_NOT_READY" for name, ok in values.items() if not ok)
        return ReadinessDecision(
            ReadinessStatus.READY if not reasons else ReadinessStatus.PENDING,
            reasons,
            values,
        )


class ReadinessRepository:
    def __init__(self, session_factory):
        self.session_factory = session_factory

    def persist_stage1(self, *, capture_session_id: str, decision: ReadinessDecision) -> str:
        with self.session_factory() as db:
            with db.begin():
                session = db.get(CaptureSession, capture_session_id)
                if session is None:
                    raise CaptureV2Error("CAPTURE_SESSION_NOT_FOUND")
                row = ReadinessSnapshot(
                    id=new_id(), capture_session_id=capture_session_id,
                    stage=ReadinessStage.CAPTURE_PATH_READY.value,
                    status=decision.status.value, checks=decision.checks,
                    reasons=list(decision.reasons),
                )
                db.add(row)
                if decision.status == ReadinessStatus.READY:
                    session.path_ready_at = session.path_ready_at or utcnow()
                    if session.state == CaptureSessionState.PREPARING.value:
                        session.state = CaptureSessionState.CAPTURE_PATH_READY.value
                    db.add(CaptureEvent(
                        id=new_id(), capture_session_id=capture_session_id,
                        entity_type="CAPTURE_SESSION", entity_id=capture_session_id,
                        event_type="CAPTURE_PATH_READY", source_ts=utcnow(),
                        payload={"checks": decision.checks},
                    ))
                return row.id

    def revoke_stage1(self, *, capture_session_id: str, reasons: list[str]) -> str:
        now = utcnow()
        with self.session_factory() as db:
            with db.begin():
                session = db.get(CaptureSession, capture_session_id)
                if session is None:
                    raise CaptureV2Error("CAPTURE_SESSION_NOT_FOUND")
                row = ReadinessSnapshot(
                    id=new_id(), capture_session_id=capture_session_id,
                    stage=ReadinessStage.CAPTURE_PATH_READY.value,
                    status=ReadinessStatus.REVOKED.value,
                    checks={}, reasons=list(reasons), created_at=now,
                )
                db.add(row)
                db.add(CaptureEvent(
                    id=new_id(), capture_session_id=capture_session_id,
                    entity_type="CAPTURE_SESSION", entity_id=capture_session_id,
                    event_type="READY_REVOKED", source_ts=now,
                    payload={"reasons": list(reasons)},
                ))
                return row.id
