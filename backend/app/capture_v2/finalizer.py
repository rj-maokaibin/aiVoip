from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select

from app.capture_v2.db_models import CaptureEvent, CaptureGap, CaptureSegment, CaptureSession
from app.capture_v2.errors import CaptureV2Error
from app.capture_v2.enums import GapCertainty
from app.capture_v2.repository.core import CaptureEpochRepository, CaptureGapRepository
from app.core.ids import new_id


def utcnow():
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class FinalizeResult:
    capture_epoch_id: str
    final_segments_sealed: int
    final_segments_transferred: int
    acknowledged: int
    remote_deleted: int
    kernel_drops: int | None
    durable: bool


class CaptureV2CaptureFinalizer:
    """Stop producer, seal final file, make evidence durable, then expose coverage.

    Remote deletion is GC and is not required for durability. ACKED is sufficient;
    anything below ACKED keeps evidence finalization incomplete.
    """

    def __init__(self, *, session_factory, producer_manager, pump, lease_manager):
        self.session_factory = session_factory
        self.producer_manager = producer_manager
        self.pump = pump
        self.lease_manager = lease_manager

    async def finalize(self, *, capture_session_id: str, capture_epoch_id: str,
                       capture_epoch_token: str, producer, token, token_provider=None,
                       reason: str = "NORMAL_FINALIZE") -> FinalizeResult:
        # Validate current DB authority immediately before the destructive stop.
        current = token_provider() if token_provider else token
        self.lease_manager.validate(current)
        await self.producer_manager.stop_identity(current, producer)
        stats = await self.producer_manager.read_exit_stats(capture_epoch_token)
        now = utcnow()
        with self.session_factory() as db:
            with db.begin():
                ended_now = CaptureEpochRepository(db).mark_ended(
                    capture_epoch_id, reason=reason, ended_at=now,
                    packets_captured=stats.packets_captured,
                    packets_received=stats.packets_received,
                    packets_dropped_kernel=stats.packets_dropped_kernel,
                )
                if ended_now:
                    db.add(CaptureEvent(
                        id=new_id(), capture_session_id=capture_session_id,
                        entity_type="CAPTURE_EPOCH", entity_id=capture_epoch_id,
                        event_type="CAPTURE_EPOCH_ENDED", source_ts=now,
                        payload={
                            "reason": reason,
                            "packets_captured": stats.packets_captured,
                            "packets_received": stats.packets_received,
                            "packets_dropped_kernel": stats.packets_dropped_kernel,
                        },
                    ))

        current = token_provider() if token_provider else current
        self.lease_manager.validate(current)
        result = await self.pump.run_final_once(
            capture_epoch_id=capture_epoch_id, token=current,
            token_provider=token_provider,
            producer_pid=producer.pid, producer_starttime=producer.process_starttime,
        )
        accounting_mismatch = False
        accounting_unknown = False
        with self.session_factory() as db:
            non_durable = list(db.scalars(select(CaptureSegment).where(
                CaptureSegment.capture_session_id == capture_session_id,
                CaptureSegment.state.not_in(("ACKED", "REMOTE_DELETED")),
            )))
            current_segments = list(db.scalars(select(CaptureSegment).where(
                CaptureSegment.capture_epoch_id == capture_epoch_id
            )))
            current_epoch_segments = len(current_segments)
            if stats.packets_captured is not None:
                counts = [seg.packet_count for seg in current_segments]
                if any(count is None for count in counts):
                    accounting_unknown = True
                else:
                    accounted_packets = sum(int(count or 0) for count in counts)
                    accounting_mismatch = accounted_packets != int(stats.packets_captured)
        if accounting_mismatch:
            with self.session_factory() as db:
                with db.begin():
                    existing = db.scalar(select(CaptureGap).where(
                        CaptureGap.capture_epoch_id == capture_epoch_id,
                        CaptureGap.channel == "PCAP",
                        CaptureGap.reason_code == "PCAP_PACKET_ACCOUNTING_MISMATCH",
                    ))
                    if existing is None:
                        CaptureGapRepository(db).create(
                            capture_session_id=capture_session_id,
                            capture_epoch_id=capture_epoch_id, channel="PCAP",
                            certainty=GapCertainty.POSSIBLE,
                            reason_code="PCAP_PACKET_ACCOUNTING_MISMATCH",
                            source="FINAL_PACKET_ACCOUNTING", gap_start_ts=None,
                            details={
                                "tcpdump_packets_captured": int(stats.packets_captured),
                                "segment_packet_count_sum": accounted_packets,
                                "boundary": "UNKNOWN",
                            },
                        )
        # Session durability needs BOTH:
        #   1) no known non-durable segment in any epoch, and
        #   2) the epoch just stopped is itself accounted for.
        # A prior epoch's ACKED segment must never mask a missing final segment from
        # the current epoch. A no-segment epoch is acceptable only when tcpdump
        # explicitly proves captured=0 and kernel_drop=0.
        empty_epoch_proven = (
            current_epoch_segments == 0
            and stats.packets_captured == 0
            and stats.packets_dropped_kernel == 0
        )
        current_epoch_accounted = current_epoch_segments > 0 or empty_epoch_proven
        durable = (
            not non_durable
            and current_epoch_accounted
            and not accounting_mismatch
            and not accounting_unknown
            and result.errors == 0
        )
        if durable:
            with self.session_factory() as db:
                with db.begin():
                    session = db.get(CaptureSession, capture_session_id)
                    if session is not None and session.evidence_durable_at is None:
                        event_ts = utcnow()
                        session.evidence_durable_at = event_ts
                        db.add(CaptureEvent(
                            id=new_id(), capture_session_id=capture_session_id,
                            entity_type="CAPTURE_SESSION", entity_id=capture_session_id,
                            event_type="EVIDENCE_DURABLE", source_ts=event_ts,
                            payload={
                                "capture_epoch_id": capture_epoch_id,
                                "empty_epoch_proven": empty_epoch_proven,
                            },
                        ))
        return FinalizeResult(
            capture_epoch_id=capture_epoch_id,
            final_segments_sealed=result.sealed,
            final_segments_transferred=result.transferred,
            acknowledged=result.acked,
            remote_deleted=result.deleted,
            kernel_drops=stats.packets_dropped_kernel,
            durable=durable,
        )
