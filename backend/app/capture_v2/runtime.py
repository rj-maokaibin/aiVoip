from __future__ import annotations

from pathlib import Path

from app.capture_v2.cutover.gate import CaptureV2CutoverGate
from app.capture_v2.errors import CaptureV2Error
from app.core.config import settings


def resolve_capture_engine_version() -> str:
    return str(getattr(settings, "capture_engine_version", "V1") or "V1").upper().strip()


def capture_v2_enabled() -> bool:
    return resolve_capture_engine_version() == "V2"


def assert_v1_live_capture_allowed() -> None:
    if capture_v2_enabled():
        raise CaptureV2Error(
            "CAPTURE_V2_SELECTED_V1_AUTHORITY_FORBIDDEN",
            details={"reason": "EXACTLY_ONE_CAPTURE_AUTHORITY"},
        )


def assert_v2_live_capture_allowed() -> dict:
    if not capture_v2_enabled():
        raise CaptureV2Error("CAPTURE_V2_NOT_SELECTED")
    if not bool(getattr(settings, "capture_v2_production_enabled", False)):
        raise CaptureV2Error("CAPTURE_V2_PRODUCTION_DISABLED")
    artifact = Path(getattr(settings, "capture_v2_release_gate_artifact", "/app/validation/capture_v2_release_gate.json"))
    return CaptureV2CutoverGate.require(artifact)


def capture_authority_mode() -> str:
    if not capture_v2_enabled():
        return "V1"
    try:
        assert_v2_live_capture_allowed()
    except CaptureV2Error:
        return "V2_BLOCKED"
    return "V2"
