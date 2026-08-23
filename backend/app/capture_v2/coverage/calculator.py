from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from app.capture_v2.enums import CoverageIntervalType, CoverageStatus
from app.capture_v2.errors import CaptureV2Error


@dataclass(frozen=True)
class EvidenceInterval:
    start: datetime
    end: datetime
    interval_type: CoverageIntervalType
    source_kind: str
    source_id: str | None = None
    certainty: str = "CONFIRMED"
    details: dict | None = None


@dataclass(frozen=True)
class TrackResult:
    status: CoverageStatus
    required_ms: int
    covered_ms: int
    gap_ms: int
    unknown_ms: int
    intervals: tuple[EvidenceInterval, ...]
    reasons: tuple[str, ...]


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _ms(start: datetime, end: datetime) -> int:
    start = _as_utc(start)
    end = _as_utc(end)
    return max(0, int(round((end - start).total_seconds() * 1000)))


def _duration_ms(items: list[EvidenceInterval]) -> int:
    """Round once after summing interval durations.

    Coverage normalization partitions a window at every evidence boundary. Rounding
    every partition independently is not additive and can lose/gain milliseconds
    even when the partitions exactly cover the required window. Sum at microsecond
    precision first, then round once so a fully covered partitioned window remains
    exactly fully covered.
    """
    total_us = 0
    for item in items:
        delta = _as_utc(item.end) - _as_utc(item.start)
        total_us += max(0, int(round(delta.total_seconds() * 1_000_000)))
    return max(0, int(round(total_us / 1000.0)))


def _clip(item: EvidenceInterval, start: datetime, end: datetime) -> EvidenceInterval | None:
    a = max(start, item.start)
    b = min(end, item.end)
    if b <= a:
        return None
    return EvidenceInterval(a, b, item.interval_type, item.source_kind,
                            item.source_id, item.certainty, item.details)


class CoverageCalculator:
    """Deterministic interval coverage. Traffic silence is not a gap by itself."""

    @staticmethod
    def calculate(*, required_start: datetime, required_end: datetime,
                  evidence: list[EvidenceInterval], applicable: bool = True,
                  uncertain_boundary: bool = False) -> TrackResult:
        required_start = _as_utc(required_start)
        required_end = _as_utc(required_end)
        evidence = [EvidenceInterval(
            _as_utc(item.start), _as_utc(item.end), item.interval_type,
            item.source_kind, item.source_id, item.certainty, item.details
        ) for item in evidence]
        if required_end <= required_start:
            raise CaptureV2Error("COVERAGE_WINDOW_INVALID")
        required_ms = _ms(required_start, required_end)
        if not applicable:
            return TrackResult(CoverageStatus.NOT_APPLICABLE, required_ms, 0, 0, 0, (), ())

        clipped = [x for item in evidence if (x := _clip(item, required_start, required_end))]
        boundaries = {required_start, required_end}
        for item in clipped:
            boundaries.add(item.start)
            boundaries.add(item.end)
        points = sorted(boundaries)
        normalized: list[EvidenceInterval] = []

        for a, b in zip(points, points[1:]):
            active = [i for i in clipped if i.start <= a and i.end >= b]
            if any(i.interval_type == CoverageIntervalType.GAP and i.certainty == "CONFIRMED" for i in active):
                kind = CoverageIntervalType.GAP
                source = next(i for i in active if i.interval_type == CoverageIntervalType.GAP and i.certainty == "CONFIRMED")
            elif any(i.interval_type in (CoverageIntervalType.UNKNOWN, CoverageIntervalType.GAP) for i in active):
                kind = CoverageIntervalType.UNKNOWN
                source = next(i for i in active if i.interval_type in (CoverageIntervalType.UNKNOWN, CoverageIntervalType.GAP))
            elif any(i.interval_type == CoverageIntervalType.COVERED for i in active):
                kind = CoverageIntervalType.COVERED
                source = next(i for i in active if i.interval_type == CoverageIntervalType.COVERED)
            else:
                kind = CoverageIntervalType.UNKNOWN
                source = EvidenceInterval(a, b, kind, "NO_EVIDENCE", None, "POSSIBLE", {})
            normalized.append(EvidenceInterval(
                a, b, kind, source.source_kind, source.source_id,
                source.certainty, source.details or {},
            ))

        covered_ms = _duration_ms([i for i in normalized if i.interval_type == CoverageIntervalType.COVERED])
        gap_ms = _duration_ms([i for i in normalized if i.interval_type == CoverageIntervalType.GAP])
        unknown_ms = _duration_ms([i for i in normalized if i.interval_type == CoverageIntervalType.UNKNOWN])
        reasons = []
        if gap_ms:
            reasons.append("CONFIRMED_GAP")
        if unknown_ms:
            reasons.append("UNKNOWN_COVERAGE")
        if uncertain_boundary:
            reasons.append("UNCERTAIN_GAP_BOUNDARY")

        if covered_ms == 0:
            status = CoverageStatus.FAILED
        elif gap_ms or unknown_ms or uncertain_boundary or covered_ms < required_ms:
            status = CoverageStatus.PARTIAL
        else:
            status = CoverageStatus.COMPLETE
        return TrackResult(status, required_ms, covered_ms, gap_ms, unknown_ms,
                           tuple(normalized), tuple(reasons))
