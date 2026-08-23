from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import or_, select

from app.capture_v2.db_models import CaptureEpoch, CaptureGap, CaptureSegment
from app.capture_v2.enums import CoverageIntervalType
from app.capture_v2.coverage.calculator import EvidenceInterval


class PcapCoverageEvidenceBuilder:
    """Build PCAP coverage from producer epochs/gaps, never from filename cadence."""

    DURABLE_STATES = {"PERSISTED", "ACK_PENDING", "ACKED", "REMOTE_DELETED"}

    def __init__(self, session_factory):
        self.session_factory = session_factory

    @staticmethod
    def _utc(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @classmethod
    def _overlaps(cls, start: datetime | None, end: datetime | None,
                  required_start: datetime, required_end: datetime) -> bool:
        start = cls._utc(start)
        end = cls._utc(end)
        required_start = cls._utc(required_start)
        required_end = cls._utc(required_end)
        if start is None or required_start is None or required_end is None:
            return False
        effective_end = end or required_end
        return start < required_end and effective_end > required_start

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

        overlapping_epoch_ids: set[str] = set()
        for epoch in epochs:
            start = epoch.started_at
            end = epoch.ended_at or required_end
            if not self._overlaps(start, end, required_start, required_end):
                continue
            overlapping_epoch_ids.add(str(epoch.id))
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
            # closed epoch overlapping this required window without final drop
            # statistics cannot be proven complete. Historical epochs outside the
            # required window must not contaminate the current CoverageWindow.
            if epoch.packets_dropped_kernel is not None and int(epoch.packets_dropped_kernel) > 0:
                uncertain = True
                reasons.append("KERNEL_CAPTURE_DROP")
            elif epoch.ended_at is not None and epoch.packets_dropped_kernel is None:
                uncertain = True
                reasons.append("KERNEL_DROP_STATUS_UNKNOWN")

        for gap in gaps:
            if gap.gap_start_ts is None or gap.gap_end_ts is None:
                # An unbounded gap is only relevant when it belongs to an epoch
                # which overlaps this CoverageWindow; otherwise a historical gap
                # from the same long-lived CaptureSession must not downgrade it.
                if str(getattr(gap, "capture_epoch_id", "") or "") in overlapping_epoch_ids:
                    uncertain = True
                    reasons.append(gap.reason_code)
                continue
            if not self._overlaps(gap.gap_start_ts, gap.gap_end_ts, required_start, required_end):
                continue
            evidence.append(EvidenceInterval(
                gap.gap_start_ts, gap.gap_end_ts,
                CoverageIntervalType.GAP if gap.certainty == "CONFIRMED" else CoverageIntervalType.UNKNOWN,
                "CAPTURE_GAP", gap.id, gap.certainty,
                {"reason_code": gap.reason_code},
            ))

        # A capture plane can be continuous while evidence is not yet durable. Final
        # completeness therefore cannot be COMPLETE while known sealed segments that
        # overlap this required window remain pre-PERSISTED. A segment without packet
        # timestamps inherits relevance from its owning epoch. Historical non-durable
        # segments outside the requested window must not contaminate current coverage.
        relevant_non_durable = []
        for segment in non_durable:
            if segment.first_packet_ts is not None or segment.last_packet_ts is not None:
                seg_start = segment.first_packet_ts or segment.last_packet_ts
                seg_end = segment.last_packet_ts or segment.first_packet_ts
                seg_start_utc = self._utc(seg_start)
                seg_end_utc = self._utc(seg_end)
                required_start_utc = self._utc(required_start)
                required_end_utc = self._utc(required_end)
                if seg_start_utc is not None and seg_end_utc is not None and seg_end_utc <= seg_start_utc:
                    # A single-timestamp segment is treated as relevant if the point
                    # falls within the required interval.
                    relevant = bool(
                        required_start_utc is not None and required_end_utc is not None
                        and required_start_utc <= seg_start_utc < required_end_utc
                    )
                else:
                    relevant = self._overlaps(seg_start, seg_end, required_start, required_end)
            else:
                relevant = str(segment.capture_epoch_id) in overlapping_epoch_ids
            if relevant:
                relevant_non_durable.append(segment)
        if relevant_non_durable:
            uncertain = True
            reasons.append("SEGMENT_NOT_DURABLE")
        return evidence, uncertain, reasons
