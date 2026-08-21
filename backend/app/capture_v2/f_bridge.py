from __future__ import annotations

import hashlib
import json

from sqlalchemy import select

from app.capture_v2.db_models import CoverageWindow, QualitySnapshot, SignalAvailability
from app.capture_v2.enums import CaptureCompleteness
from app.capture_v2.errors import CaptureV2Error
from app.capture_v2.quality.confidence import ConfidenceInput, DiagnosticConfidenceEvaluator
from app.capture_v2.quality.signals import SignalAvailabilityEvaluator, SignalEvidence
from app.capture_v2.quality.snapshot import QualitySnapshotRepository
from app.capture_v2.report.evidence_first import EvidenceFirstReportBuilder, FindingEvidenceRequest


class CaptureV2FQualityReporter:
    def __init__(self, session_factory):
        self.session_factory = session_factory
        self.snapshots = QualitySnapshotRepository(session_factory)
        self.reports = EvidenceFirstReportBuilder(session_factory)

    def evaluate_from_coverage(
        self, *, coverage_window_id: str, capture_session_id: str,
        capture_attempt_id: str | None, call_ref: str | None,
        signals: list[SignalEvidence],
        required_channels_for_diagnosis: tuple[str, ...],
        independent_support_count: int,
        contradictions: tuple[str, ...] = (),
        policy_version: str = "capture-quality-v2.1",
    ) -> tuple[str, dict]:
        with self.session_factory() as db:
            window = db.get(CoverageWindow, coverage_window_id)
            if window is None or window.capture_session_id != capture_session_id:
                raise CaptureV2Error("QUALITY_COVERAGE_WINDOW_NOT_FOUND")
            if window.finalized_at is None or window.status not in ("COMPLETE", "PARTIAL", "FAILED"):
                raise CaptureV2Error(
                    "QUALITY_REQUIRES_FINALIZED_COVERAGE",
                    details={"coverage_window_id": coverage_window_id, "status": window.status},
                )
            capture_completeness = window.status
        return self._evaluate_and_persist(
            coverage_window_id=coverage_window_id,
            capture_session_id=capture_session_id,
            capture_attempt_id=capture_attempt_id, call_ref=call_ref,
            capture_completeness=capture_completeness,
            signals=signals,
            required_channels_for_diagnosis=required_channels_for_diagnosis,
            independent_support_count=independent_support_count,
            contradictions=contradictions, policy_version=policy_version,
        )

    def evaluate_and_persist(
        self, *, capture_session_id: str,
        capture_attempt_id: str | None, call_ref: str | None,
        capture_completeness: str,
        signals: list[SignalEvidence],
        required_channels_for_diagnosis: tuple[str, ...],
        independent_support_count: int,
        contradictions: tuple[str, ...] = (),
        policy_version: str = "capture-quality-v2.1",
    ) -> tuple[str, dict]:
        """Low-level/test compatibility API.

        Production orchestration must call evaluate_from_coverage so completeness
        is loaded from the deterministic Coverage Ledger rather than supplied by
        an analyzer/LLM.
        """
        return self._evaluate_and_persist(
            coverage_window_id=None,
            capture_session_id=capture_session_id,
            capture_attempt_id=capture_attempt_id, call_ref=call_ref,
            capture_completeness=capture_completeness, signals=signals,
            required_channels_for_diagnosis=required_channels_for_diagnosis,
            independent_support_count=independent_support_count,
            contradictions=contradictions, policy_version=policy_version,
        )

    def _evaluate_and_persist(
        self, *, coverage_window_id: str | None, capture_session_id: str,
        capture_attempt_id: str | None, call_ref: str | None,
        capture_completeness: str, signals: list[SignalEvidence],
        required_channels_for_diagnosis: tuple[str, ...],
        independent_support_count: int, contradictions: tuple[str, ...],
        policy_version: str,
    ) -> tuple[str, dict]:
        signal_decisions = [SignalAvailabilityEvaluator.evaluate(s) for s in signals]
        availability = {d.channel: d.availability for d in signal_decisions}
        confidence = DiagnosticConfidenceEvaluator.evaluate(ConfidenceInput(
            capture_completeness=CaptureCompleteness(capture_completeness),
            signal_availability=availability,
            required_channels_for_diagnosis=required_channels_for_diagnosis,
            independent_support_count=independent_support_count,
            contradictions=contradictions,
        ))
        semantic_payload = {
            "coverage_window_id": coverage_window_id,
            "capture_session_id": capture_session_id,
            "capture_attempt_id": capture_attempt_id,
            "call_ref": call_ref,
            "capture_completeness": capture_completeness,
            "policy_version": policy_version,
            "signals": [
                {
                    "channel": d.channel,
                    "availability": d.availability.value,
                    "reason_code": d.reason_code,
                    "details": d.details,
                }
                for d in signal_decisions
            ],
            "required_channels_for_diagnosis": list(required_channels_for_diagnosis),
            "independent_support_count": int(independent_support_count),
            "contradictions": list(contradictions),
        }
        digest = hashlib.sha256(
            json.dumps(semantic_payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        ).hexdigest()
        snapshot_id = self.snapshots.persist(
            coverage_window_id=coverage_window_id,
            capture_session_id=capture_session_id,
            capture_attempt_id=capture_attempt_id, call_ref=call_ref,
            capture_completeness=capture_completeness,
            confidence=confidence, policy_version=policy_version,
            signals=signal_decisions,
            idempotency_key=f"quality:{digest}",
        )
        return snapshot_id, {
            "coverage_window_id": coverage_window_id,
            "capture_completeness": capture_completeness,
            "diagnostic_confidence": confidence.confidence.value,
            "confidence_reasons": list(confidence.reasons),
            "signals": {d.channel: d.availability.value for d in signal_decisions},
        }

    def build_report_from_snapshot(
        self, *, capture_session_id: str, quality_snapshot_id: str,
        findings: list[FindingEvidenceRequest],
    ) -> dict:
        with self.session_factory() as db:
            row = db.get(QualitySnapshot, quality_snapshot_id)
            if row is None or row.capture_session_id != capture_session_id:
                raise CaptureV2Error("QUALITY_SNAPSHOT_NOT_FOUND")
            signals = {
                item.channel: item.availability
                for item in db.scalars(select(SignalAvailability).where(
                    SignalAvailability.quality_snapshot_id == quality_snapshot_id
                ))
            }
            quality = {
                "quality_snapshot_id": row.id,
                "coverage_window_id": row.coverage_window_id,
                "capture_completeness": row.capture_completeness,
                "diagnostic_confidence": row.diagnostic_confidence,
                "confidence_reasons": list(row.reasons or []),
                "signals": signals,
                "policy_version": row.policy_version,
            }
        return self.reports.build(
            capture_session_id=capture_session_id, quality=quality, findings=findings,
        )

    def build_report(self, *, capture_session_id: str, quality: dict,
                     findings: list[FindingEvidenceRequest]) -> dict:
        """Compatibility helper; production should use build_report_from_snapshot."""
        return self.reports.build(
            capture_session_id=capture_session_id, quality=quality, findings=findings,
        )
