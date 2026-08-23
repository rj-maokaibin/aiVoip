from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from app.capture_v2.db_models import CaptureSegment


@dataclass(frozen=True)
class SpoolPressure:
    unacked_bytes: int
    oldest_unacked_seconds: float
    state: str
    reasons: tuple[str, ...]


class SpoolPressureEvaluator:
    def __init__(self, session_factory):
        self.session_factory = session_factory

    def evaluate(self, *, capture_session_id: str, max_unacked_bytes: int | None,
                 max_oldest_unacked_seconds: float | None) -> SpoolPressure:
        with self.session_factory() as db:
            rows = list(db.query(CaptureSegment).filter(
                CaptureSegment.capture_session_id == capture_session_id,
                CaptureSegment.state.notin_(("ACKED", "REMOTE_DELETED")),
            ).all())
        total = sum(int(r.remote_size or 0) for r in rows)
        now = datetime.now(timezone.utc)
        ages = []
        for r in rows:
            ts = r.discovered_at
            if ts is None:
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            ages.append(max(0.0, (now - ts).total_seconds()))
        oldest = max(ages, default=0.0)
        reasons = []
        if max_unacked_bytes is not None and total > int(max_unacked_bytes):
            reasons.append("UNACKED_BYTES_LIMIT")
        if max_oldest_unacked_seconds is not None and oldest > float(max_oldest_unacked_seconds):
            reasons.append("UNACKED_AGE_LIMIT")
        return SpoolPressure(total, oldest, "CRITICAL" if reasons else "NORMAL", tuple(reasons))
