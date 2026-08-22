from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import delete, func, select

from app.capture_v2.coverage.calculator import EvidenceInterval
from app.capture_v2.coverage.ledger import CoverageLedgerService
from app.capture_v2.db_models import CaptureInterval if False else CoverageInterval
from app.capture_v2.db_models import CaptureSession, CoverageTrack, CoverageWindow
from app.capture_v2.enums import CoverageIntervalType, CoverageStatus
from app.capture_v2.errors import CaptureV2Error
from app.db.session import SessionLocal


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def validate_real_postgres_coverage_ledger(*, device_id: str, marker: str) -> dict:
    """Exercise the real CoverageLedgerService against the configured PostgreSQL DB.

    This is deliberately a validation-only DB self-test. It does not claim that a
    historical or current call was finalized online. The latest real CaptureSession
    is used only as a valid FK anchor. All rows created by this function are tagged,
    snapshotted into the returned result, and deleted before returning.
    """
    with SessionLocal() as db:
        bind = db.get_bind()
        dialect = str(bind.dialect.name)
        capture = db.scalar(
            select(CaptureSession)
            .where(CaptureSession.device_id == device_id)
            .order_by(CaptureSession.created_at.desc())
            .limit(1)
        )
        capture_session_id = str(capture.id) if capture is not None else None

    if dialect != "postgresql":
        return {
            "verdict": "INCONCLUSIVE",
            "reason": "REAL_POSTGRESQL_REQUIRED",
            "dialect": dialect,
            "release_gate_effect": "VALIDATES_LEDGER_RUNTIME_ONLY_NOT_ONLINE_R5_PASS",
        }
    if not capture_session_id:
        return {
            "verdict": "INCONCLUSIVE",
            "reason": "CAPTURE_SESSION_FK_ANCHOR_NOT_FOUND",
            "dialect": dialect,
            "release_gate_effect": "VALIDATES_LEDGER_RUNTIME_ONLY_NOT_ONLINE_R5_PASS",
        }

    service = CoverageLedgerService(SessionLocal)
    token = uuid4().hex[:12]
    key = f"r5-db-selftest:{device_id[:8]}:{token}"
    start = _utcnow() - timedelta(seconds=10)
    end = start + timedelta(seconds=4)
    details = {
        "validation_only": True,
        "validation_kind": "R5_REAL_POSTGRES_COVERAGE_LEDGER_SELF_TEST",
        "marker": str(marker)[:80],
    }
    window_id: str | None = None
    track_ids: list[str] = []
    checks: dict[str, bool] = {}
    snapshot: dict = {}

    try:
        window_id = service.create_window(
            capture_session_id=capture_session_id,
            capture_attempt_id=None,
            call_ref=f"validation-{token}",
            window_type="VALIDATION_DB_SELF_TEST",
            required_start_ts=start,
            required_end_ts=end,
            details=details,
            idempotency_key=key,
        )
        same_id = service.create_window(
            capture_session_id=capture_session_id,
            capture_attempt_id=None,
            call_ref=f"validation-{token}",
            window_type="VALIDATION_DB_SELF_TEST",
            required_start_ts=start,
            required_end_ts=end,
            details=details,
            idempotency_key=key,
        )
        checks["idempotent_create_same_window"] = same_id == window_id

        full = [EvidenceInterval(
            start=start,
            end=end,
            interval_type=CoverageIntervalType.COVERED,
            source_kind="VALIDATION_SELF_TEST",
            source_id=token,
            certainty="CONFIRMED",
            details={"synthetic_interval": True, "real_db_runtime": True},
        )]
        for channel in ("PCAP", "PCM_RX", "PCM_TX"):
            result = service.calculate_track(
                coverage_window_id=window_id,
                channel=channel,
                requirement="REQUIRED",
                evidence=full,
            )
            checks[f"{channel.lower()}_complete"] = result.status == CoverageStatus.COMPLETE

        first_final = service.finalize_window(window_id)
        checks["first_finalize_complete"] = first_final == CoverageStatus.COMPLETE

        # Recalculation must invalidate the prior finalization and fail closed.
        half = [EvidenceInterval(
            start=start,
            end=start + timedelta(seconds=2),
            interval_type=CoverageIntervalType.COVERED,
            source_kind="VALIDATION_SELF_TEST",
            source_id=token,
            certainty="CONFIRMED",
            details={"phase": "partial_recalculation"},
        )]
        partial = service.calculate_track(
            coverage_window_id=window_id,
            channel="PCAP",
            requirement="REQUIRED",
            evidence=half,
        )
        checks["recalculation_is_partial"] = partial.status == CoverageStatus.PARTIAL
        with SessionLocal() as db:
            pending = db.get(CoverageWindow, window_id)
            checks["recalculation_invalidates_finalization"] = bool(
                pending is not None and pending.status == CoverageStatus.PENDING.value
                and pending.finalized_at is None
            )
        second_final = service.finalize_window(window_id)
        checks["second_finalize_partial"] = second_final == CoverageStatus.PARTIAL

        restored = service.calculate_track(
            coverage_window_id=window_id,
            channel="PCAP",
            requirement="REQUIRED",
            evidence=full,
        )
        checks["restored_track_complete"] = restored.status == CoverageStatus.COMPLETE
        third_final = service.finalize_window(window_id)
        checks["third_finalize_complete"] = third_final == CoverageStatus.COMPLETE

        conflict_code = None
        try:
            service.create_window(
                capture_session_id=capture_session_id,
                capture_attempt_id=None,
                call_ref=f"validation-{token}",
                window_type="VALIDATION_DB_SELF_TEST",
                required_start_ts=start,
                required_end_ts=end + timedelta(seconds=1),
                details=details,
                idempotency_key=key,
            )
        except CaptureV2Error as exc:
            conflict_code = exc.code
        checks["idempotency_conflict_fails_closed"] = conflict_code == "COVERAGE_WINDOW_IDEMPOTENCY_CONFLICT"

        with SessionLocal() as db:
            window = db.get(CoverageWindow, window_id)
            tracks = list(db.scalars(select(CoverageTrack).where(CoverageTrack.coverage_window_id == window_id)))
            track_ids = [str(t.id) for t in tracks]
            interval_count = int(db.scalar(
                select(func.count(CoverageInterval.id)).where(CoverageInterval.coverage_track_id.in_(track_ids))
            ) or 0) if track_ids else 0
            snapshot = {
                "window_id": window_id,
                "capture_session_fk_anchor": capture_session_id,
                "window_status": window.status if window is not None else None,
                "finalized_at_present": bool(window and window.finalized_at),
                "track_count": len(tracks),
                "track_statuses": {t.channel: t.status for t in tracks},
                "interval_count": interval_count,
                "dialect": dialect,
            }
            checks["three_required_tracks_persisted"] = len(tracks) == 3
            checks["intervals_persisted"] = interval_count >= 3
            checks["final_row_complete"] = bool(
                window is not None and window.status == CoverageStatus.COMPLETE.value and window.finalized_at
            )
    finally:
        if window_id:
            with SessionLocal() as db:
                with db.begin():
                    db.execute(delete(CoverageWindow).where(CoverageWindow.id == window_id))
            with SessionLocal() as db:
                window_left = db.get(CoverageWindow, window_id) is not None
                tracks_left = int(db.scalar(
                    select(func.count(CoverageTrack.id)).where(CoverageTrack.coverage_window_id == window_id)
                ) or 0)
                intervals_left = int(db.scalar(
                    select(func.count(CoverageInterval.id)).where(CoverageInterval.coverage_track_id.in_(track_ids))
                ) or 0) if track_ids else 0
            checks["cleanup_window_removed"] = not window_left
            checks["cleanup_tracks_cascade_removed"] = tracks_left == 0
            checks["cleanup_intervals_cascade_removed"] = intervals_left == 0

    passed = all(checks.values()) and bool(checks)
    return {
        "verdict": "PASS" if passed else "FAIL",
        "reason": "REAL_POSTGRES_COVERAGE_LEDGER_SELF_TEST_COMPLETED" if passed else "REAL_POSTGRES_COVERAGE_LEDGER_SELF_TEST_FAILED",
        "release_gate_effect": "VALIDATES_LEDGER_RUNTIME_ONLY_NOT_ONLINE_R5_PASS",
        "checks": checks,
        "snapshot_before_cleanup": snapshot,
        "cleanup_verified": bool(
            checks.get("cleanup_window_removed")
            and checks.get("cleanup_tracks_cascade_removed")
            and checks.get("cleanup_intervals_cascade_removed")
        ),
    }
