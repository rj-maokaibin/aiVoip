from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable

from .models import GateCaseResult, GateCheck, GateVerdict


class RollbackPhase(StrEnum):
    PRE_V1 = "PRE_V1"
    V2_ACTIVE = "V2_ACTIVE"
    ROLLED_BACK_V1 = "ROLLED_BACK_V1"


class RollbackEvidenceKind(StrEnum):
    REAL_DUT = "REAL_DUT"
    SIMULATED = "SIMULATED"
    SYNTHETIC = "SYNTHETIC"


class RollbackActivationMode(StrEnum):
    V1 = "V1"
    ACTIVATION_REHEARSAL = "ACTIVATION_REHEARSAL"
    PRODUCTION = "PRODUCTION"


def _strict_bool(raw: dict[str, Any], key: str) -> bool:
    value = raw.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"ROLLBACK_{key.upper()}_BOOLEAN_REQUIRED")
    return value


@dataclass(frozen=True)
class RollbackObservation:
    phase: RollbackPhase
    observed_at: datetime
    capture_engine_version: str
    capture_v2_production_enabled: bool
    activation_mode: RollbackActivationMode
    v1_healthy: bool
    v2_producer_count: int
    evidence_kind: RollbackEvidenceKind
    evidence_refs: tuple[str, ...]

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "RollbackObservation":
        if not isinstance(raw, dict):
            raise ValueError("ROLLBACK_OBSERVATION_NOT_OBJECT")
        try:
            observed_at = datetime.fromisoformat(str(raw["observed_at"]).replace("Z", "+00:00"))
        except Exception as exc:
            raise ValueError("ROLLBACK_OBSERVED_AT_INVALID") from exc
        if observed_at.tzinfo is None:
            raise ValueError("ROLLBACK_OBSERVED_AT_TZ_REQUIRED")
        refs = raw.get("evidence_refs")
        if not isinstance(refs, (list, tuple)):
            raise ValueError("ROLLBACK_EVIDENCE_REFS_INVALID")
        refs_tuple = tuple(str(ref).strip() for ref in refs if str(ref).strip())
        count = raw.get("v2_producer_count")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError("ROLLBACK_V2_PRODUCER_COUNT_INVALID")
        try:
            activation_mode = RollbackActivationMode(str(raw["activation_mode"]))
        except KeyError as exc:
            raise ValueError("ROLLBACK_ACTIVATION_MODE_REQUIRED") from exc
        except ValueError as exc:
            raise ValueError("ROLLBACK_ACTIVATION_MODE_INVALID") from exc
        return cls(
            phase=RollbackPhase(str(raw["phase"])),
            observed_at=observed_at,
            capture_engine_version=str(raw["capture_engine_version"]),
            capture_v2_production_enabled=_strict_bool(raw, "capture_v2_production_enabled"),
            activation_mode=activation_mode,
            v1_healthy=_strict_bool(raw, "v1_healthy"),
            v2_producer_count=count,
            evidence_kind=RollbackEvidenceKind(str(raw["evidence_kind"])),
            evidence_refs=refs_tuple,
        )

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["phase"] = self.phase.value
        payload["observed_at"] = self.observed_at.isoformat()
        payload["activation_mode"] = self.activation_mode.value
        payload["evidence_kind"] = self.evidence_kind.value
        payload["evidence_refs"] = list(self.evidence_refs)
        return payload


class R7RollbackRehearsalGate:
    """Validate a real-DUT pre-cutover activation/rollback rehearsal.

    The release rollback gate must be provable *before* Production V2 is enabled.
    Therefore V2_ACTIVE may be one of two explicit modes:

    - ACTIVATION_REHEARSAL: engine V2, Production flag false, exact production
      runtime path enabled only by the bounded rehearsal interlock.
    - PRODUCTION: engine V2, Production flag true, accepted if such evidence exists.

    Ambiguous ``V2 + production=false`` without the explicit rehearsal mode never
    passes. This removes the historical circular dependency where Production V2
    required rollback_gate_passed=true while the rollback validator itself required
    Production V2=true.
    """

    GATE_ID = "R7-ROLLBACK-REHEARSAL"
    EVIDENCE_SCHEMA = "capture-v2-r7-rollback-evidence-v2"
    REQUIRED_PHASES = (
        RollbackPhase.PRE_V1,
        RollbackPhase.V2_ACTIVE,
        RollbackPhase.ROLLED_BACK_V1,
    )

    @classmethod
    def evaluate_artifact(cls, raw: dict[str, Any]) -> GateCaseResult:
        if not isinstance(raw, dict) or raw.get("schema_version") != cls.EVIDENCE_SCHEMA:
            return GateCaseResult(
                gate_id=cls.GATE_ID,
                verdict=GateVerdict.INCONCLUSIVE,
                checks=(GateCheck(
                    "evidence_artifact_schema",
                    None,
                    cls.EVIDENCE_SCHEMA,
                    raw.get("schema_version") if isinstance(raw, dict) else type(raw).__name__,
                ),),
                summary="Rollback evidence artifact schema is invalid.",
                facts={"reason": "ROLLBACK_EVIDENCE_ARTIFACT_SCHEMA_INVALID"},
            )
        rows = raw.get("observations")
        if not isinstance(rows, list):
            return GateCaseResult(
                gate_id=cls.GATE_ID,
                verdict=GateVerdict.INCONCLUSIVE,
                checks=(GateCheck("observations_array", None, "list", type(rows).__name__),),
                summary="Rollback evidence observations are missing or invalid.",
                facts={"reason": "ROLLBACK_OBSERVATIONS_ARRAY_INVALID"},
            )
        return cls.evaluate_dicts(rows)

    @classmethod
    def evaluate_dicts(cls, rows: Iterable[dict[str, Any]]) -> GateCaseResult:
        try:
            observations = tuple(RollbackObservation.from_dict(row) for row in rows)
        except (KeyError, TypeError, ValueError) as exc:
            return GateCaseResult(
                gate_id=cls.GATE_ID,
                verdict=GateVerdict.INCONCLUSIVE,
                checks=(GateCheck(
                    name="evidence_schema_valid",
                    passed=None,
                    expected="valid rollback observation schema",
                    observed=str(exc),
                ),),
                summary=f"Rollback evidence is not structurally valid: {exc}",
                facts={"reason": str(exc)},
            )
        return cls.evaluate(observations)

    @classmethod
    def evaluate(cls, observations: Iterable[RollbackObservation]) -> GateCaseResult:
        observations = tuple(observations)
        by_phase: dict[RollbackPhase, list[RollbackObservation]] = {
            phase: [] for phase in cls.REQUIRED_PHASES
        }
        for observation in observations:
            if observation.phase in by_phase:
                by_phase[observation.phase].append(observation)

        phase_counts = {phase.value: len(by_phase[phase]) for phase in cls.REQUIRED_PHASES}
        if any(count != 1 for count in phase_counts.values()):
            missing = [phase for phase, count in phase_counts.items() if count == 0]
            duplicate = [phase for phase, count in phase_counts.items() if count > 1]
            reason = "ROLLBACK_REAL_PHASES_MISSING" if missing else "ROLLBACK_PHASE_EVIDENCE_AMBIGUOUS"
            verdict = GateVerdict.DEFERRED_REAL_GATE if missing else GateVerdict.INCONCLUSIVE
            return GateCaseResult(
                gate_id=cls.GATE_ID,
                verdict=verdict,
                checks=(GateCheck(
                    name="exactly_one_observation_per_phase",
                    passed=None,
                    expected={phase.value: 1 for phase in cls.REQUIRED_PHASES},
                    observed=phase_counts,
                    details={"missing": missing, "duplicate": duplicate},
                ),),
                summary=f"Rollback rehearsal cannot pass: {reason}",
                facts={"reason": reason, "phase_counts": phase_counts},
            )

        ordered = tuple(by_phase[phase][0] for phase in cls.REQUIRED_PHASES)
        all_real = all(obs.evidence_kind == RollbackEvidenceKind.REAL_DUT for obs in ordered)
        all_have_refs = all(obs.evidence_refs for obs in ordered)
        all_refs = [ref for obs in ordered for ref in obs.evidence_refs]
        refs_unique_across_phases = len(all_refs) == len(set(all_refs))

        if not all_real or not all_have_refs:
            return GateCaseResult(
                gate_id=cls.GATE_ID,
                verdict=GateVerdict.DEFERRED_REAL_GATE,
                checks=(
                    GateCheck("real_dut_evidence", all_real, True, all_real),
                    GateCheck("evidence_refs_present", all_have_refs, True, all_have_refs),
                ),
                summary="Rollback rehearsal needs non-empty REAL_DUT evidence for every phase.",
                facts={
                    "reason": "ROLLBACK_REAL_DUT_EVIDENCE_REQUIRED",
                    "observations": [obs.as_dict() for obs in ordered],
                },
            )

        if not refs_unique_across_phases:
            return GateCaseResult(
                gate_id=cls.GATE_ID,
                verdict=GateVerdict.INCONCLUSIVE,
                checks=(GateCheck(
                    "evidence_refs_unique_across_phases",
                    None,
                    "distinct evidence refs per phase",
                    all_refs,
                ),),
                summary="Rollback evidence reuses an evidence reference across phases.",
                facts={"reason": "ROLLBACK_EVIDENCE_REF_REUSED"},
            )

        chronological = ordered[0].observed_at < ordered[1].observed_at < ordered[2].observed_at
        if not chronological:
            return GateCaseResult(
                gate_id=cls.GATE_ID,
                verdict=GateVerdict.INCONCLUSIVE,
                checks=(GateCheck(
                    "phase_source_time_order",
                    None,
                    "PRE_V1 < V2_ACTIVE < ROLLED_BACK_V1",
                    [obs.observed_at.isoformat() for obs in ordered],
                ),),
                summary="Rollback evidence is not strictly chronological.",
                facts={"reason": "ROLLBACK_EVIDENCE_TIME_ORDER_INVALID"},
            )

        pre, v2, rolled = ordered
        v2_rehearsal = (
            v2.capture_engine_version == "V2"
            and not v2.capture_v2_production_enabled
            and v2.activation_mode == RollbackActivationMode.ACTIVATION_REHEARSAL
        )
        v2_production = (
            v2.capture_engine_version == "V2"
            and v2.capture_v2_production_enabled
            and v2.activation_mode == RollbackActivationMode.PRODUCTION
        )
        v2_phase_valid = v2_rehearsal or v2_production
        checks = (
            GateCheck(
                "pre_v1_authoritative",
                pre.capture_engine_version == "V1"
                and not pre.capture_v2_production_enabled
                and pre.activation_mode == RollbackActivationMode.V1,
                {"capture_engine_version": "V1", "production_v2": False, "activation_mode": "V1"},
                {"capture_engine_version": pre.capture_engine_version,
                 "production_v2": pre.capture_v2_production_enabled,
                 "activation_mode": pre.activation_mode.value},
            ),
            GateCheck("pre_v1_healthy", pre.v1_healthy, True, pre.v1_healthy),
            GateCheck("pre_no_v2_producer", pre.v2_producer_count == 0, 0, pre.v2_producer_count),
            GateCheck(
                "v2_phase_observed",
                v2_phase_valid,
                "explicit ACTIVATION_REHEARSAL or PRODUCTION V2 authority",
                {"capture_engine_version": v2.capture_engine_version,
                 "production_v2": v2.capture_v2_production_enabled,
                 "activation_mode": v2.activation_mode.value},
                details={"rehearsal": v2_rehearsal, "production": v2_production},
            ),
            GateCheck("v2_producer_observed", v2.v2_producer_count >= 1, ">=1", v2.v2_producer_count),
            GateCheck(
                "rollback_v1_authoritative",
                rolled.capture_engine_version == "V1"
                and not rolled.capture_v2_production_enabled
                and rolled.activation_mode == RollbackActivationMode.V1,
                {"capture_engine_version": "V1", "production_v2": False, "activation_mode": "V1"},
                {"capture_engine_version": rolled.capture_engine_version,
                 "production_v2": rolled.capture_v2_production_enabled,
                 "activation_mode": rolled.activation_mode.value},
            ),
            GateCheck("rollback_v1_healthy", rolled.v1_healthy, True, rolled.v1_healthy),
            GateCheck("rollback_no_v2_producer", rolled.v2_producer_count == 0, 0, rolled.v2_producer_count),
        )
        failed = [check.name for check in checks if check.passed is False]
        verdict = GateVerdict.FAIL if failed else GateVerdict.PASS
        reason = "ROLLBACK_REHEARSAL_STATE_INVALID" if failed else "ROLLBACK_REHEARSAL_PROVEN"
        return GateCaseResult(
            gate_id=cls.GATE_ID,
            verdict=verdict,
            checks=checks,
            summary=(
                "Real-DUT evidence proves bounded V2 authority activation, rollback, and V1 health restoration."
                if verdict == GateVerdict.PASS
                else "Real-DUT rollback evidence contains failed state/health checks."
            ),
            facts={
                "reason": reason,
                "failed_checks": failed,
                "v2_activation_mode": v2.activation_mode.value,
                "release_gate_scope": (
                    "PRE_CUTOVER_ACTIVATION_REHEARSAL"
                    if v2_rehearsal else "PRODUCTION_ROLLBACK_OBSERVATION"
                ),
                "observations": [obs.as_dict() for obs in ordered],
            },
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate Capture V2 R7 rollback rehearsal evidence")
    parser.add_argument("--evidence", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        raw = json.loads(args.evidence.read_text(encoding="utf-8"))
    except Exception as exc:
        result = GateCaseResult(
            gate_id=R7RollbackRehearsalGate.GATE_ID,
            verdict=GateVerdict.INCONCLUSIVE,
            checks=(GateCheck("evidence_file_readable", None, True, str(exc)),),
            summary="Rollback evidence file cannot be read as JSON.",
            facts={"reason": "ROLLBACK_EVIDENCE_FILE_INVALID"},
        )
    else:
        result = R7RollbackRehearsalGate.evaluate_artifact(raw)
    print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
    if result.verdict == GateVerdict.PASS:
        return 0
    if result.verdict in {GateVerdict.INCONCLUSIVE, GateVerdict.DEFERRED_REAL_GATE}:
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
