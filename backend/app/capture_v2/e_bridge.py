from __future__ import annotations

from datetime import timedelta

from app.capture_v2.coverage.calculator import EvidenceInterval
from app.capture_v2.coverage.ledger import CoverageLedgerService
from app.capture_v2.coverage.pcap_source import PcapCoverageEvidenceBuilder
from app.capture_v2.db_models import CaptureAttempt, CaptureSession
from app.capture_v2.enums import CoverageStatus
from app.capture_v2.errors import CaptureV2Error
from app.capture_v2.segment.retention import SegmentRetentionService


class CaptureV2ECoverageFinalizer:
    def __init__(self, session_factory):
        self.session_factory = session_factory
        self.ledger = CoverageLedgerService(session_factory)
        self.pcap = PcapCoverageEvidenceBuilder(session_factory)
        self.retention = SegmentRetentionService(session_factory)

    def finalize_attempt(self, *, capture_session_id: str, capture_attempt_id: str,
                         call_ref: str | None,
                         channel_evidence: dict[str, list[EvidenceInterval]],
                         channel_applicability: dict[str, bool] | None = None) -> tuple[str, CoverageStatus]:
        channel_applicability = channel_applicability or {}
        with self.session_factory() as db:
            attempt = db.get(CaptureAttempt, capture_attempt_id)
            session = db.get(CaptureSession, capture_session_id)
            if attempt is None or session is None:
                raise CaptureV2Error("COVERAGE_SUBJECT_NOT_FOUND")
            start = attempt.confirmed_start_source_ts or attempt.candidate_start_source_ts
            end = attempt.ended_source_ts
            if start is None or end is None or end <= start:
                raise CaptureV2Error("ATTEMPT_WINDOW_NOT_FINALIZABLE")
            effective = dict(session.effective_profile or {})
            resolved = dict(effective.get("resolved") or effective)
            policy = dict(resolved.get("coverage") or {})
            requirements = dict(resolved.get("channels") or {})
            pre = float(policy.get("pre_trigger_seconds") or 0)
            post = float(policy.get("post_trigger_seconds") or 0)
        required_start = start - timedelta(seconds=pre)
        required_end = end + timedelta(seconds=post)
        window_id = self.ledger.create_window(
            capture_session_id=capture_session_id, capture_attempt_id=capture_attempt_id,
            call_ref=call_ref, window_type="ATTEMPT_EVIDENCE",
            required_start_ts=required_start, required_end_ts=required_end,
            details={"pre_trigger_seconds": pre, "post_trigger_seconds": post},
            idempotency_key=f"attempt:{capture_attempt_id}:ATTEMPT_EVIDENCE",
        )

        pcap_evidence, pcap_uncertain, pcap_reasons = self.pcap.build(
            capture_session_id=capture_session_id,
            required_start=required_start, required_end=required_end,
        )
        self.ledger.calculate_track(
            coverage_window_id=window_id, channel="PCAP",
            requirement=str(requirements.get("pcap", "REQUIRED")), evidence=pcap_evidence,
            applicable=True, uncertain_boundary=pcap_uncertain,
        )

        for channel, requirement in requirements.items():
            upper = channel.upper()
            if upper == "PCAP" or upper == "DEBUG":
                continue
            applicable = channel_applicability.get(upper, True)
            evidence = channel_evidence.get(upper, [])
            self.ledger.calculate_track(
                coverage_window_id=window_id, channel=upper,
                requirement=str(requirement), evidence=evidence,
                applicable=applicable, uncertain_boundary=False,
            )
        status = self.ledger.finalize_window(window_id)
        # Pin the complete CaptureEpoch(s) overlapping the deterministic window.
        # This is intentionally conservative and keeps silent/header-only PCAPs.
        self.retention.pin_for_coverage_window(window_id)
        return window_id, status
