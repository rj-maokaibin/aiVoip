from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.capture_v2.db_models import AttemptDataPlaneVerification, CaptureAttempt
from app.capture_v2.enums import VerificationStatus
from app.capture_v2.errors import CaptureV2Error
from app.core.ids import new_id


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    """Normalize DB/source timestamps to aware UTC.

    SQLite commonly returns timezone-aware SQLAlchemy DateTime columns as naive
    values. Production PostgreSQL preserves the offset, but deterministic capture
    logic must behave identically in both cases and must never compare naive and
    aware datetimes. A naive value stored by this subsystem is defined as UTC.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class ChannelExpectation:
    channel: str
    timeout_seconds: float | None
    applicable: bool = True
    reason_if_na: str | None = None


class AttemptDataPlaneVerifier:
    """Expectation-driven verification; no global OFFHOOK+N timeout exists."""

    def __init__(self, session_factory):
        self.session_factory = session_factory

    def create_expectation(self, *, capture_attempt_id: str, expectation: ChannelExpectation,
                           expectation_created_at: datetime | None = None,
                           details: dict | None = None) -> str:
        created = _as_utc(expectation_created_at or utcnow())
        with self.session_factory() as db:
            with db.begin():
                if db.get(CaptureAttempt, capture_attempt_id) is None:
                    raise CaptureV2Error("CAPTURE_ATTEMPT_NOT_FOUND")
                existing = db.scalar(select(AttemptDataPlaneVerification).where(
                    AttemptDataPlaneVerification.capture_attempt_id == capture_attempt_id,
                    AttemptDataPlaneVerification.channel == expectation.channel,
                ))
                if existing is not None:
                    expected_status = (VerificationStatus.PENDING.value
                                       if expectation.applicable else VerificationStatus.NOT_APPLICABLE.value)
                    expected_deadline = None
                    if expectation.applicable and expectation.timeout_seconds is not None:
                        expected_deadline = created + timedelta(seconds=float(expectation.timeout_seconds))
                    same_deadline = (
                        existing.verification_deadline is None and expected_deadline is None
                    ) or (
                        existing.verification_deadline is not None and expected_deadline is not None
                        and _as_utc(existing.verification_deadline) == expected_deadline
                    )
                    same_created = _as_utc(existing.expectation_created_at) == created
                    same_details = dict(existing.details or {}) == dict(details or {})
                    # Status may have advanced after creation; idempotency is based
                    # on the immutable expectation contract, not current outcome.
                    same_applicability = (
                        (existing.status == VerificationStatus.NOT_APPLICABLE.value)
                        == (expected_status == VerificationStatus.NOT_APPLICABLE.value)
                    )
                    if same_deadline and same_created and same_details and same_applicability:
                        return existing.id
                    raise CaptureV2Error(
                        "CHANNEL_EXPECTATION_CONFLICT",
                        details={"channel": expectation.channel},
                    )
                status = VerificationStatus.PENDING.value if expectation.applicable else VerificationStatus.NOT_APPLICABLE.value
                deadline = None
                if expectation.applicable and expectation.timeout_seconds is not None:
                    deadline = created + timedelta(seconds=float(expectation.timeout_seconds))
                row = AttemptDataPlaneVerification(
                    id=new_id(), capture_attempt_id=capture_attempt_id,
                    channel=expectation.channel, status=status,
                    expectation_created_at=created, verification_deadline=deadline,
                    reason_code=expectation.reason_if_na if not expectation.applicable else None,
                    details=details or {},
                )
                db.add(row)
                return row.id

    def observe(self, *, capture_attempt_id: str, channel: str,
                source_ts: datetime, details: dict | None = None) -> None:
        with self.session_factory() as db:
            with db.begin():
                row = db.scalar(select(AttemptDataPlaneVerification).where(
                    AttemptDataPlaneVerification.capture_attempt_id == capture_attempt_id,
                    AttemptDataPlaneVerification.channel == channel,
                ))
                if row is None:
                    raise CaptureV2Error("CHANNEL_EXPECTATION_NOT_FOUND", details={"channel": channel})
                if row.status == VerificationStatus.NOT_APPLICABLE.value:
                    raise CaptureV2Error("CHANNEL_EXPECTATION_NOT_APPLICABLE", details={"channel": channel})
                seen = _as_utc(source_ts)
                if row.status == VerificationStatus.VERIFIED.value:
                    return
                if row.status == VerificationStatus.MISSING.value:
                    # A parser/transfer may deliver evidence after the wall-clock
                    # deadline even though the packet/event Source Time proves it
                    # existed before the expectation expired. Deterministic source
                    # time is authoritative over processing time.
                    deadline = _as_utc(row.verification_deadline) if row.verification_deadline else None
                    if deadline is not None and seen > deadline:
                        row.details = {
                            **(row.details or {}), **(details or {}),
                            "late_seen_source_ts": seen.isoformat(),
                            "late_after_deadline": True,
                        }
                        return
                    row.details = {
                        **(row.details or {}), **(details or {}),
                        "corrected_from_missing_by_source_time": True,
                    }
                elif row.status not in (VerificationStatus.PENDING.value, VerificationStatus.DEGRADED.value):
                    return
                row.status = VerificationStatus.VERIFIED.value
                row.first_seen_source_ts = row.first_seen_source_ts or seen
                row.reason_code = None
                row.details = {**(row.details or {}), **(details or {})}

    def expire_due(self, *, capture_attempt_id: str, now: datetime | None = None) -> tuple[str, ...]:
        now = _as_utc(now or utcnow())
        missing = []
        with self.session_factory() as db:
            with db.begin():
                rows = list(db.scalars(select(AttemptDataPlaneVerification).where(
                    AttemptDataPlaneVerification.capture_attempt_id == capture_attempt_id,
                    AttemptDataPlaneVerification.status == VerificationStatus.PENDING.value,
                )))
                for row in rows:
                    if (row.verification_deadline is not None
                            and now >= _as_utc(row.verification_deadline)):
                        row.status = VerificationStatus.MISSING.value
                        row.reason_code = "CHANNEL_EXPECTATION_TIMEOUT"
                        missing.append(row.channel)
        return tuple(missing)

    def snapshot(self, capture_attempt_id: str) -> dict[str, str]:
        with self.session_factory() as db:
            rows = list(db.scalars(select(AttemptDataPlaneVerification).where(
                AttemptDataPlaneVerification.capture_attempt_id == capture_attempt_id,
            )))
        return {row.channel: row.status for row in rows}
