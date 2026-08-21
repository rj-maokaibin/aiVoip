from __future__ import annotations

from dataclasses import dataclass

from app.capture_v2.enums import SignalAvailabilityStatus, VerificationStatus


@dataclass(frozen=True)
class SignalEvidence:
    channel: str
    expected: bool
    verification_status: str | None = None
    captured: bool = False
    usable: bool = False
    encrypted: bool = False
    degraded: bool = False
    reason_code: str | None = None
    details: dict | None = None


@dataclass(frozen=True)
class SignalDecision:
    channel: str
    availability: SignalAvailabilityStatus
    reason_code: str | None
    details: dict


class SignalAvailabilityEvaluator:
    @staticmethod
    def evaluate(item: SignalEvidence) -> SignalDecision:
        details = dict(item.details or {})
        if not item.expected:
            return SignalDecision(
                item.channel, SignalAvailabilityStatus.UNAVAILABLE_NOT_APPLICABLE,
                item.reason_code or "NOT_APPLICABLE", details,
            )
        if item.encrypted:
            return SignalDecision(
                item.channel, SignalAvailabilityStatus.UNAVAILABLE_ENCRYPTED,
                item.reason_code or "ENCRYPTED", details,
            )
        if item.degraded:
            return SignalDecision(
                item.channel, SignalAvailabilityStatus.DEGRADED,
                item.reason_code or "SIGNAL_DEGRADED", details,
            )
        if item.verification_status == VerificationStatus.NOT_APPLICABLE.value:
            return SignalDecision(
                item.channel, SignalAvailabilityStatus.UNAVAILABLE_NOT_APPLICABLE,
                item.reason_code or "NOT_APPLICABLE", details,
            )
        if item.verification_status in (VerificationStatus.MISSING.value, VerificationStatus.DEGRADED.value):
            return SignalDecision(
                item.channel,
                SignalAvailabilityStatus.UNAVAILABLE_NOT_CAPTURED
                if item.verification_status == VerificationStatus.MISSING.value
                else SignalAvailabilityStatus.DEGRADED,
                item.reason_code or item.verification_status,
                details,
            )
        if item.captured and item.usable:
            return SignalDecision(item.channel, SignalAvailabilityStatus.AVAILABLE, None, details)
        if item.captured and not item.usable:
            return SignalDecision(
                item.channel, SignalAvailabilityStatus.DEGRADED,
                item.reason_code or "CAPTURED_NOT_USABLE", details,
            )
        return SignalDecision(
            item.channel, SignalAvailabilityStatus.UNAVAILABLE_NOT_CAPTURED,
            item.reason_code or "NOT_CAPTURED", details,
        )
