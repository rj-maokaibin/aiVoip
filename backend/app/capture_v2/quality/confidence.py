from __future__ import annotations

from dataclasses import dataclass

from app.capture_v2.enums import CaptureCompleteness, DiagnosticConfidence, SignalAvailabilityStatus


@dataclass(frozen=True)
class ConfidenceInput:
    capture_completeness: CaptureCompleteness
    signal_availability: dict[str, SignalAvailabilityStatus]
    required_channels_for_diagnosis: tuple[str, ...]
    independent_support_count: int = 0
    contradictions: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConfidenceDecision:
    confidence: DiagnosticConfidence
    reasons: tuple[str, ...]


class DiagnosticConfidenceEvaluator:
    """Deterministic confidence ceiling. AI cannot promote above this result."""

    @staticmethod
    def evaluate(value: ConfidenceInput) -> ConfidenceDecision:
        reasons = []
        if value.contradictions:
            return ConfidenceDecision(
                DiagnosticConfidence.LOW,
                tuple(["EVIDENCE_CONTRADICTION", *value.contradictions]),
            )
        if value.capture_completeness == CaptureCompleteness.FAILED:
            return ConfidenceDecision(DiagnosticConfidence.INSUFFICIENT, ("CAPTURE_FAILED",))

        missing_required = []
        degraded_required = []
        for channel in value.required_channels_for_diagnosis:
            status = value.signal_availability.get(channel, SignalAvailabilityStatus.UNKNOWN)
            if status in (
                SignalAvailabilityStatus.UNAVAILABLE_NOT_CAPTURED,
                SignalAvailabilityStatus.UNAVAILABLE_ENCRYPTED,
                SignalAvailabilityStatus.UNKNOWN,
            ):
                missing_required.append(f"{channel}:{status.value}")
            elif status == SignalAvailabilityStatus.DEGRADED:
                degraded_required.append(channel)

        if missing_required:
            return ConfidenceDecision(
                DiagnosticConfidence.LOW,
                tuple(["REQUIRED_SIGNAL_UNAVAILABLE", *missing_required]),
            )
        if value.capture_completeness == CaptureCompleteness.PARTIAL:
            reasons.append("CAPTURE_PARTIAL")
            if degraded_required:
                reasons.append("REQUIRED_SIGNAL_DEGRADED")
            return ConfidenceDecision(DiagnosticConfidence.MEDIUM, tuple(reasons))
        if degraded_required:
            return ConfidenceDecision(
                DiagnosticConfidence.MEDIUM,
                tuple(["REQUIRED_SIGNAL_DEGRADED", *degraded_required]),
            )
        if value.independent_support_count >= 2:
            return ConfidenceDecision(DiagnosticConfidence.HIGH, ())
        if value.independent_support_count == 1:
            return ConfidenceDecision(DiagnosticConfidence.MEDIUM, ("SINGLE_EVIDENCE_SOURCE",))
        return ConfidenceDecision(DiagnosticConfidence.LOW, ("NO_INDEPENDENT_SUPPORT",))
