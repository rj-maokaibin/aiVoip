from __future__ import annotations

import os
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


def _release_artifact_path() -> Path:
    return Path(
        getattr(
            settings,
            "capture_v2_release_gate_artifact",
            "/app/validation/capture_v2_release_gate.json",
        )
    )


def assert_v2_live_capture_allowed() -> dict:
    if not capture_v2_enabled():
        raise CaptureV2Error("CAPTURE_V2_NOT_SELECTED")
    if not bool(getattr(settings, "capture_v2_production_enabled", False)):
        raise CaptureV2Error("CAPTURE_V2_PRODUCTION_DISABLED")
    return CaptureV2CutoverGate.require(_release_artifact_path())


def assert_v2_activation_rehearsal_allowed() -> dict:
    """Allow real V2 authority only for the explicit pre-cutover rollback rehearsal.

    This is intentionally narrower than Production activation.  It requires V2 to
    be selected, Production V2 to remain disabled, an explicit process-level
    ``CAPTURE_V2_ACTIVATION_REHEARSAL=true`` switch, and every release gate except
    the very rollback gate this rehearsal exists to prove.  No code path may infer
    rollback success from validation-scope rehearsals.
    """
    if not capture_v2_enabled():
        raise CaptureV2Error("CAPTURE_V2_NOT_SELECTED")
    if bool(getattr(settings, "capture_v2_production_enabled", False)):
        raise CaptureV2Error("CAPTURE_V2_REHEARSAL_REQUIRES_PRODUCTION_DISABLED")
    enabled = str(os.getenv("CAPTURE_V2_ACTIVATION_REHEARSAL", "false")).lower().strip()
    if enabled not in {"1", "true", "yes", "on"}:
        raise CaptureV2Error("CAPTURE_V2_ACTIVATION_REHEARSAL_DISABLED")

    decision = CaptureV2CutoverGate.evaluate(_release_artifact_path())
    artifact = dict(decision.artifact or {})
    if artifact.get("schema_version") != "capture-v2-release-gate-v1":
        raise CaptureV2Error(
            "CAPTURE_V2_REHEARSAL_GATE_BLOCKED",
            details={"reasons": list(decision.reasons or ("CUTOVER_GATE_SCHEMA_INVALID",))},
        )
    required = tuple(
        key for key in CaptureV2CutoverGate.REQUIRED_TRUE if key != "rollback_gate_passed"
    )
    missing = [key.upper() + "_FALSE" for key in required if artifact.get(key) is not True]
    if missing:
        raise CaptureV2Error(
            "CAPTURE_V2_REHEARSAL_GATE_BLOCKED", details={"reasons": missing}
        )
    if artifact.get("rollback_gate_passed") is True:
        raise CaptureV2Error(
            "CAPTURE_V2_REHEARSAL_NO_LONGER_REQUIRED",
            details={"reason": "ROLLBACK_GATE_ALREADY_PASSED"},
        )
    return artifact


def assert_selected_v2_live_capture_allowed() -> dict:
    """Return the selected V2 authority mode after fail-closed validation."""
    if bool(getattr(settings, "capture_v2_production_enabled", False)):
        artifact = assert_v2_live_capture_allowed()
        return {"mode": "PRODUCTION", "artifact": artifact}
    artifact = assert_v2_activation_rehearsal_allowed()
    return {"mode": "ACTIVATION_REHEARSAL", "artifact": artifact}


def capture_authority_mode() -> str:
    if not capture_v2_enabled():
        return "V1"
    try:
        selected = assert_selected_v2_live_capture_allowed()
    except CaptureV2Error:
        return "V2_BLOCKED"
    return "V2" if selected["mode"] == "PRODUCTION" else "V2_REHEARSAL"
