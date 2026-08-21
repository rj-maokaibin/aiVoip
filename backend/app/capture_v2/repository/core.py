from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.capture_v2.db_models import CaptureEpoch, CaptureEvent, CaptureGap, CaptureSession
from app.capture_v2.enums import CaptureEpochState, CaptureEventType, GapCertainty
from app.capture_v2.errors import CaptureV2Error


class CaptureSessionRepository:
    def __init__(self, db: Session):
        self.db = db

    def create(self, **values) -> CaptureSession:
        row = CaptureSession(**values)
        self.db.add(row)
        self.db.flush()
        return row

    def get(self, session_id: str) -> CaptureSession | None:
        return self.db.get(CaptureSession, session_id)

    def for_reproduction(self, reproduction_session_id: str) -> CaptureSession | None:
        return self.db.execute(
            select(CaptureSession).where(
                CaptureSession.reproduction_session_id == reproduction_session_id
            )
        ).scalars().first()

    def transition(self, session_id: str, *, expected: str, next_state: str, **values) -> None:
        result = self.db.execute(
            update(CaptureSession)
            .where(CaptureSession.id == session_id, CaptureSession.state == expected)
            .values(state=next_state, **values)
        )
        if result.rowcount != 1:
            raise CaptureV2Error(
                "CAPTURE_STATE_CONFLICT",
                details={"capture_session_id": session_id, "expected": expected, "next": next_state},
            )
        self.db.flush()


class CaptureEventRepository:
    def __init__(self, db: Session):
        self.db = db

    def append(
        self,
        *,
        capture_session_id: str,
        event_type: str | CaptureEventType,
        entity_type: str,
        entity_id: str | None = None,
        source_ts: datetime | None = None,
        payload: dict | None = None,
    ) -> CaptureEvent:
        row = CaptureEvent(
            capture_session_id=capture_session_id,
            event_type=event_type.value if isinstance(event_type, CaptureEventType) else str(event_type),
            entity_type=entity_type,
            entity_id=entity_id,
            source_ts=source_ts,
            payload=payload,
        )
        self.db.add(row)
        self.db.flush()
        return row


class CaptureEpochRepository:
    def __init__(self, db: Session):
        self.db = db

    def running_for_session(self, capture_session_id: str) -> CaptureEpoch | None:
        return self.db.execute(
            select(CaptureEpoch)
            .where(
                CaptureEpoch.capture_session_id == capture_session_id,
                CaptureEpoch.state.in_([CaptureEpochState.STARTING.value, CaptureEpochState.RUNNING.value]),
            )
            .order_by(CaptureEpoch.epoch_index.desc())
        ).scalars().first()

    def next_index(self, capture_session_id: str) -> int:
        value = self.db.execute(
            select(func.max(CaptureEpoch.epoch_index)).where(CaptureEpoch.capture_session_id == capture_session_id)
        ).scalar_one_or_none()
        return int(value or 0) + 1

    def create_starting(
        self,
        *,
        capture_session_id: str,
        device_id: str,
        epoch_index: int,
        epoch_token: str,
        boot_id: str | None,
        interface: str,
        lease_epoch: int,
    ) -> CaptureEpoch:
        row = CaptureEpoch(
            capture_session_id=capture_session_id,
            device_id=device_id,
            epoch_index=epoch_index,
            epoch_token=epoch_token,
            boot_id=boot_id,
            interface=interface,
            lease_epoch_started=lease_epoch,
            state=CaptureEpochState.STARTING.value,
        )
        self.db.add(row)
        self.db.flush()
        return row

    def mark_running(self, epoch_id: str, *, pid: int, starttime: int, cmdline: str) -> None:
        result = self.db.execute(
            update(CaptureEpoch)
            .where(CaptureEpoch.id == epoch_id, CaptureEpoch.state == CaptureEpochState.STARTING.value)
            .values(
                state=CaptureEpochState.RUNNING.value,
                producer_pid=pid,
                producer_starttime=starttime,
                producer_cmdline=cmdline,
            )
        )
        if result.rowcount != 1:
            raise CaptureV2Error("CAPTURE_EPOCH_STATE_CONFLICT", details={"capture_epoch_id": epoch_id})
        self.db.flush()

    def mark_ended(self, epoch_id: str, *, reason: str, ended_at: datetime, failed: bool = False,
                   packets_captured: int | None = None, packets_received: int | None = None,
                   packets_dropped_kernel: int | None = None) -> bool:
        result = self.db.execute(
            update(CaptureEpoch)
            .where(
                CaptureEpoch.id == epoch_id,
                CaptureEpoch.state.in_([CaptureEpochState.STARTING.value, CaptureEpochState.RUNNING.value]),
            )
            .values(
                state=CaptureEpochState.FAILED.value if failed else CaptureEpochState.ENDED.value,
                ended_at=ended_at,
                end_reason=reason,
                packets_captured=packets_captured,
                packets_received=packets_received,
                packets_dropped_kernel=packets_dropped_kernel,
            )
        )
        first_transition = result.rowcount == 1
        if not first_transition:
            row = self.db.get(CaptureEpoch, epoch_id)
            if row is None:
                raise CaptureV2Error("CAPTURE_EPOCH_NOT_FOUND", details={"capture_epoch_id": epoch_id})
            if row.state not in (CaptureEpochState.ENDED.value, CaptureEpochState.FAILED.value):
                raise CaptureV2Error(
                    "CAPTURE_EPOCH_STATE_CONFLICT",
                    details={"capture_epoch_id": epoch_id, "state": row.state},
                )
            # Finalizer retry may observe tcpdump stderr after the first ENDED commit.
            # Fill only previously-unknown counters; never rewrite committed counts.
            if row.packets_captured is None and packets_captured is not None:
                row.packets_captured = packets_captured
            if row.packets_received is None and packets_received is not None:
                row.packets_received = packets_received
            if row.packets_dropped_kernel is None and packets_dropped_kernel is not None:
                row.packets_dropped_kernel = packets_dropped_kernel
        self.db.flush()
        return first_transition



class CaptureGapRepository:
    def __init__(self, db: Session):
        self.db = db

    def create(
        self,
        *,
        capture_session_id: str,
        capture_epoch_id: str | None,
        channel: str,
        certainty: str | GapCertainty,
        reason_code: str,
        source: str,
        gap_start_ts: datetime | None = None,
        details: dict | None = None,
    ) -> CaptureGap:
        row = CaptureGap(
            capture_session_id=capture_session_id,
            capture_epoch_id=capture_epoch_id,
            channel=channel,
            certainty=certainty.value if isinstance(certainty, GapCertainty) else str(certainty),
            reason_code=reason_code,
            source=source,
            gap_start_ts=gap_start_ts,
            details=details,
        )
        self.db.add(row)
        self.db.flush()
        return row

    def close(self, gap_id: str, *, gap_end_ts: datetime, recovered_at: datetime) -> None:
        self.db.execute(
            update(CaptureGap)
            .where(CaptureGap.id == gap_id, CaptureGap.gap_end_ts.is_(None))
            .values(gap_end_ts=gap_end_ts, recovered_at=recovered_at)
        )
        self.db.flush()
