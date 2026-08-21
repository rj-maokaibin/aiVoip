from __future__ import annotations

from sqlalchemy import delete, select

from app.capture_v2.db_models import QualitySnapshot, SignalAvailability
from app.capture_v2.errors import CaptureV2Error
from app.capture_v2.quality.confidence import ConfidenceDecision
from app.capture_v2.quality.signals import SignalDecision
from app.core.ids import new_id


class QualitySnapshotRepository:
    """Idempotent final-quality persistence.

    Same idempotency key + same semantic payload returns the same snapshot. A key
    collision with different semantics fails closed instead of silently rewriting
    diagnostic history.
    """

    def __init__(self, session_factory):
        self.session_factory = session_factory

    def persist(self, *, capture_session_id: str, capture_attempt_id: str | None,
                call_ref: str | None, capture_completeness: str,
                coverage_window_id: str | None,
                confidence: ConfidenceDecision, policy_version: str,
                signals: list[SignalDecision], idempotency_key: str) -> str:
        with self.session_factory() as db:
            existing = db.scalar(select(QualitySnapshot).where(
                QualitySnapshot.idempotency_key == idempotency_key
            ))
            if existing is not None:
                expected_signals = {
                    s.channel: (s.availability.value, s.reason_code, s.details or {})
                    for s in signals
                }
                actual_signals = {
                    s.channel: (s.availability, s.reason_code, s.details or {})
                    for s in db.scalars(select(SignalAvailability).where(
                        SignalAvailability.quality_snapshot_id == existing.id
                    ))
                }
                same = (
                    existing.capture_session_id == capture_session_id
                    and existing.coverage_window_id == coverage_window_id
                    and existing.capture_attempt_id == capture_attempt_id
                    and existing.call_ref == call_ref
                    and existing.capture_completeness == capture_completeness
                    and existing.diagnostic_confidence == confidence.confidence.value
                    and existing.policy_version == policy_version
                    and list(existing.reasons or []) == list(confidence.reasons)
                    and actual_signals == expected_signals
                )
                if not same:
                    raise CaptureV2Error(
                        "QUALITY_SNAPSHOT_IDEMPOTENCY_CONFLICT",
                        details={"idempotency_key": idempotency_key, "quality_snapshot_id": existing.id},
                    )
                return existing.id

        with self.session_factory() as db:
            with db.begin():
                row = QualitySnapshot(
                    id=new_id(), idempotency_key=idempotency_key,
                    coverage_window_id=coverage_window_id,
                    capture_session_id=capture_session_id,
                    capture_attempt_id=capture_attempt_id, call_ref=call_ref,
                    capture_completeness=capture_completeness,
                    diagnostic_confidence=confidence.confidence.value,
                    policy_version=policy_version, reasons=list(confidence.reasons),
                )
                db.add(row)
                db.flush()
                for signal in signals:
                    db.add(SignalAvailability(
                        id=new_id(), quality_snapshot_id=row.id,
                        channel=signal.channel, availability=signal.availability.value,
                        reason_code=signal.reason_code, details=signal.details,
                    ))
                return row.id
