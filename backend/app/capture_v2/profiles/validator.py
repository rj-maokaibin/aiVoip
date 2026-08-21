from __future__ import annotations

from app.capture_v2.errors import CaptureV2Error
from app.capture_v2.profiles.schema import CaptureProfile, PlatformProfile


def validate_invariants(profile: CaptureProfile, platform: PlatformProfile) -> None:
    if profile.capture.mode != "FULL_VOICE":
        raise CaptureV2Error("PROFILE_SAFETY_VIOLATION", details={"field": "capture.mode"})
    if profile.capture.snaplen != 0:
        raise CaptureV2Error("PROFILE_SAFETY_VIOLATION", details={"field": "capture.snaplen"})
    if profile.capture.segment_seconds != 5:
        raise CaptureV2Error("PROFILE_SAFETY_VIOLATION", details={"field": "capture.segment_seconds"})
    if not profile.transfer.server_sha256:
        raise CaptureV2Error("PROFILE_SAFETY_VIOLATION", details={"field": "transfer.server_sha256"})
    if profile.transfer.parallelism > platform.resource.max_transfer_parallelism:
        raise CaptureV2Error(
            "PROFILE_PLATFORM_LIMIT_EXCEEDED",
            details={
                "field": "transfer.parallelism",
                "requested": profile.transfer.parallelism,
                "maximum": platform.resource.max_transfer_parallelism,
            },
        )
