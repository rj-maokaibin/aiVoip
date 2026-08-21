from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo

from app.capture_v2.errors import CaptureV2Error


@dataclass(frozen=True)
class TimelineStamp:
    source_time: datetime | None
    collector_time: datetime
    processing_time: datetime
    authority: str


def normalize_utc(value: datetime) -> datetime:
    """Normalize DB/transport timestamps to timezone-aware UTC.

    SQLite commonly returns naive datetimes even for timezone=True columns;
    V2 source-time ordering must therefore never compare raw ORM datetimes.
    Naive values are interpreted as UTC because Capture V2 persists all server
    timestamps in UTC.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def parse_aim_source_time(value: str, *, device_timezone: tzinfo | None) -> datetime:
    try:
        parsed = datetime.strptime(value.strip(), "%Y-%m-%d %H:%M:%S.%f")
    except ValueError as exc:
        raise CaptureV2Error("SOURCE_TIME_PARSE_FAILED", details={"value": value}) from exc
    if device_timezone is None:
        raise CaptureV2Error("SOURCE_TIME_TIMEZONE_REQUIRED")
    return parsed.replace(tzinfo=device_timezone)


def authoritative_stamp(*, source_time: datetime | None,
                        collector_time: datetime | None = None,
                        processing_time: datetime | None = None) -> TimelineStamp:
    collector_time = collector_time or datetime.now(timezone.utc)
    processing_time = processing_time or datetime.now(timezone.utc)
    authority = "SOURCE_TIME" if source_time is not None else "COLLECTOR_TIME"
    return TimelineStamp(source_time, collector_time, processing_time, authority)
