from __future__ import annotations

from datetime import datetime

from sqlalchemy import or_, select

from app.capture_v2.db_models import CaptureEpoch, CaptureGap, CaptureSegment
from app.capture_v2.enums import CoverageIntervalType
from app.capture_v2.coverage.calculator import EvidenceInterval


class PcapCoverageEvidenceBuilder:
    """Build PCAP coverage from producer epochs/gaps, never from filename cadence."""

    DURABLE_STATES = {"PERSISTED", "ACK_PENDING", "ACKED", "REMOTE_DELETED"}

    def __init__(self, session_factory):
        self.session_factory = session_factory

    def build(self, *, capture_session_id: str, required_start: datetime,
              required_end: datetime) -> tuple[list[EvidenceInterval], bool, list[str]]:
        evidence: list[EvidenceInterval] = []
        uncertain = False
        reasons: list[str] = []
        with self.session_factory() as db:
            epochs = list(db.scalars(select(CaptureEpoch).where(
                CaptureEpoch.capture_session_id == capture_session_id,
            ).order_by(CaptureEpoch.started_at)))
            gaps = list(db.scalars(select(CaptureGap).where(
                CaptureGap.capture_session_id == capture_session_id,
                CaptureGap.channel == "PCAP",
            )))
            non_durable = list(db.scalars(select(CaptureSegment).where(
                CaptureSegment.capture_session_id == capture_session_id,
                or_(
                    CaptureSegment.state.not_in(tuple(self.DURABLE_STATES)),
                    CaptureSegment.last_error_code == "SERVER_COPY_MISSING",
                ),
            )))

        for epoch in epochs:
            start = epoch.started_at
            end = epoch.ended_at or required_end
            if start is not None and end is not None and end > start:
                evidence.append(EvidenceInterval(
                    start, end, CoverageIntervalType.COVERED,
                    "CAPTURE_EPOCH", epoch.id, "CONFIRMED",
                    {"epoch_token": epoch.epoch_token, "state": epoch.state,
                     "packets_dropped_kernel": epoch.packets_dropped_kernel},
                ))
            # A running producer interval does not prove every packet reached the
            # capture file when the kernel reported drops. Because tcpdump only
            # gives an aggregate count, the exact lost interval is unknowable; do
            # not fabricate one, but cap completeness at PARTIAL. Likewise, a
            # closed epoch without final drop statistics cannot be proven complete.
            if epoch.packets_dropped_kernel is not None and int(epoch.packets_dropped_kernel) > 0:
                uncertain = True
                reasons.append("KERNEL_CAPTURE_DROP")
            elif epoch.ended_at is not None and epoch.packets_dropped_kernel is None:
                uncertain = True
                reasons.append("KERNEL_DROP_STATUS_UNKNOWN")

        for gap in gaps:
            if gap.gap_start_ts is None or gap.gap_end_ts is None:
                uncertain = True
                reasons.append(gap.reason_code)
                continue
            evidence.append(EvidenceInterval(
                gap.gap_start_ts, gap.gap_end_ts,
                CoverageIntervalType.GAP if gap.certainty == "CONFIRMED" else CoverageIntervalType.UNKNOWN,
                "CAPTURE_GAP", gap.id, gap.certainty,
                {"reason_code": gap.reason_code},
            ))

        # A capture plane can be continuous while evidence is not yet durable. Final
        # completeness therefore cannot be COMPLETE while known sealed segments remain
        # pre-PERSISTED. We do not invent a timestamp interval if the segment has no packets.
        if non_durable:
            uncertain = True
            reasons.append("SEGMENT_NOT_DURABLE")
        return evidence, uncertain, reasons
