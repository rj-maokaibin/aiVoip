from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from app.capture_v2.errors import CaptureV2Error


@dataclass(frozen=True)
class CutoverGateDecision:
    allowed: bool
    reasons: tuple[str, ...]
    artifact: dict | None = None


class CaptureV2CutoverGate:
    REQUIRED_TRUE = (
        "software_gate_passed",
        "real_ownership_gate_passed",
        "real_segment_gate_passed",
        "readiness_gate_passed",
        "coverage_gate_passed",
        "e2e_gate_passed",
        "rollback_gate_passed",
        "approved",
    )

    @classmethod
    def evaluate(cls, artifact_path: Path) -> CutoverGateDecision:
        path = Path(artifact_path)
        if not path.is_file():
            return CutoverGateDecision(False, ("CUTOVER_GATE_ARTIFACT_MISSING",), None)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return CutoverGateDecision(False, ("CUTOVER_GATE_ARTIFACT_INVALID",), None)
        if raw.get("schema_version") != "capture-v2-release-gate-v1":
            return CutoverGateDecision(False, ("CUTOVER_GATE_SCHEMA_INVALID",), raw)
        reasons = tuple(key.upper() + "_FALSE" for key in cls.REQUIRED_TRUE if raw.get(key) is not True)
        return CutoverGateDecision(not reasons, reasons, raw)

    @classmethod
    def require(cls, artifact_path: Path) -> dict:
        decision = cls.evaluate(artifact_path)
        if not decision.allowed:
            raise CaptureV2Error("CAPTURE_V2_PRODUCTION_GATE_BLOCKED", details={"reasons": list(decision.reasons)})
        return dict(decision.artifact or {})
