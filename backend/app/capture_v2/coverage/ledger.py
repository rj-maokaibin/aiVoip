from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import delete, select

from app.capture_v2.coverage.calculator import CoverageCalculator, EvidenceInterval, TrackResult
from app.capture_v2.db_models import CoverageInterval, CoverageTrack, CoverageWindow
from app.capture_v2.enums import CoverageStatus
from app.capture_v2.errors import CaptureV2Error
from app.core.ids import new_id


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class CoverageLedgerService:
    """Deterministic/idempotent coverage ledger.

    Retrying the same logical window returns the same row. Recalculating a channel
    replaces that track's normalized intervals in one transaction; it never appends
    duplicate tracks or intervals.
    """

    def __init__(self, session_factory):
        self.session_factory = session_factory

    def create_window(self, *, capture_session_id: str, capture_attempt_id: str | None,
                      call_ref: str | None, window_type: str,
                      required_start_ts: datetime, required_end_ts: datetime,
                      details: dict | None = None, idempotency_key: str | None = None) -> str:
        key = idempotency_key or (
            f"{capture_session_id}:{capture_attempt_id or '-'}:{call_ref or '-'}:{window_type}:"
            f"{required_start_ts.isoformat()}:{required_end_ts.isoformat()}"
        )
        with self.session_factory() as db:
            existing = db.scalar(select(CoverageWindow).where(CoverageWindow.idempotency_key == key))
            if existing is not None:
                same = (
                    existing.capture_session_id == capture_session_id
                    and existing.capture_attempt_id == capture_attempt_id
                    and existing.call_ref == call_ref
                    and existing.window_type == window_type
                    and _aware_utc(existing.required_start_ts) == _aware_utc(required_start_ts)
                    and _aware_utc(existing.required_end_ts) == _aware_utc(required_end_ts)
                )
                if not same:
                    raise CaptureV2Error(
                        "COVERAGE_WINDOW_IDEMPOTENCY_CONFLICT",
                        details={"idempotency_key": key, "coverage_window_id": existing.id},
                    )
                return existing.id
        with self.session_factory() as db:
            with db.begin():
                row = CoverageWindow(
                    id=new_id(), idempotency_key=key,
                    capture_session_id=capture_session_id,
                    capture_attempt_id=capture_attempt_id, call_ref=call_ref,
                    window_type=window_type, required_start_ts=required_start_ts,
                    required_end_ts=required_end_ts, status="PENDING",
                    details=details or {},
                )
                db.add(row)
                db.flush()
                return row.id

    def calculate_track(self, *, coverage_window_id: str, channel: str,
                        requirement: str, evidence: list[EvidenceInterval],
                        applicable: bool = True, uncertain_boundary: bool = False) -> TrackResult:
        with self.session_factory() as db:
            window = db.get(CoverageWindow, coverage_window_id)
            if window is None:
                raise CaptureV2Error("COVERAGE_WINDOW_NOT_FOUND")
            normalized_evidence = [
                EvidenceInterval(
                    _aware_utc(item.start), _aware_utc(item.end), item.interval_type,
                    item.source_kind, item.source_id, item.certainty, item.details,
                )
                for item in evidence
            ]
            result = CoverageCalculator.calculate(
                required_start=_aware_utc(window.required_start_ts),
                required_end=_aware_utc(window.required_end_ts),
                evidence=normalized_evidence, applicable=applicable,
                uncertain_boundary=uncertain_boundary,
            )

        with self.session_factory() as db:
            with db.begin():
                track = db.scalar(select(CoverageTrack).where(
                    CoverageTrack.coverage_window_id == coverage_window_id,
                    CoverageTrack.channel == channel,
                ))
                if track is None:
                    track = CoverageTrack(
                        id=new_id(), coverage_window_id=coverage_window_id, channel=channel,
                        requirement=requirement, status=result.status.value,
                        required_ms=result.required_ms, covered_ms=result.covered_ms,
                        gap_ms=result.gap_ms, unknown_ms=result.unknown_ms,
                        details={"reasons": list(result.reasons)},
                    )
                    db.add(track)
                    db.flush()
                else:
                    track.requirement = requirement
                    track.status = result.status.value
                    track.required_ms = result.required_ms
                    track.covered_ms = result.covered_ms
                    track.gap_ms = result.gap_ms
                    track.unknown_ms = result.unknown_ms
                    track.details = {"reasons": list(result.reasons)}
                    db.execute(delete(CoverageInterval).where(
                        CoverageInterval.coverage_track_id == track.id
                    ))
                    db.flush()
                for item in result.intervals:
                    db.add(CoverageInterval(
                        id=new_id(), coverage_track_id=track.id,
                        interval_start_ts=item.start, interval_end_ts=item.end,
                        interval_type=item.interval_type.value,
                        source_kind=item.source_kind, source_id=item.source_id,
                        certainty=item.certainty, details=item.details or {},
                    ))
                window = db.get(CoverageWindow, coverage_window_id)
                if window is not None:
                    # Any deterministic recalculation invalidates the prior aggregate
                    # finalization until finalize_window is called again.
                    window.status = "PENDING"
                    window.finalized_at = None
                return result

    def finalize_window(self, coverage_window_id: str) -> CoverageStatus:
        with self.session_factory() as db:
            with db.begin():
                window = db.get(CoverageWindow, coverage_window_id)
                if window is None:
                    raise CaptureV2Error("COVERAGE_WINDOW_NOT_FOUND")
                tracks = list(db.scalars(select(CoverageTrack).where(
                    CoverageTrack.coverage_window_id == coverage_window_id
                )))
                required = [
                    t for t in tracks
                    if t.requirement in ("REQUIRED", "CONDITIONAL_REQUIRED")
                    and t.status != CoverageStatus.NOT_APPLICABLE.value
                ]
                if any(t.status == CoverageStatus.FAILED.value for t in required):
                    status = CoverageStatus.FAILED
                elif any(t.status == CoverageStatus.PARTIAL.value for t in required):
                    status = CoverageStatus.PARTIAL
                elif required and all(t.status == CoverageStatus.COMPLETE.value for t in required):
                    status = CoverageStatus.COMPLETE
                else:
                    status = CoverageStatus.PARTIAL
                window.status = status.value
                window.finalized_at = window.finalized_at or utcnow()
                return status
