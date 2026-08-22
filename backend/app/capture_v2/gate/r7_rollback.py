from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum
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


@dataclass(frozen=True)
class RollbackObservation:
    phase: RollbackPhase
    observed_at: datetime
    capture_engine_version: str
    capture_v2_production_enabled: bool
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
        return cls(
            phase=RollbackPhase(str(raw["phase"])),
            observed_at=observed_at,
            capture_engine_version=str(raw["capture_engine_version"]),
            capture_v2_production_enabled=bool(raw["capture_v2_production_enabled"]),
            v1_healthy=bool(raw["v1_healthy"]),
            v2_producer_count=count,
            evidence_kind=RollbackEvidenceKind(str(raw["evidence_kind"])),
            evidence_refs=refs_tuple,
        )

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["phase"] = self.phase.value
        payload["observed_at"] = self.observed_at.isoformat()
        payload["evidence_kind"] = self.evidence_kind.value
        payload["evidence_refs"] = list(self.evidence_refs)
        return payload


class R7RollbackRehearsalGate:
    """Validate evidence from a real V2 -> V1 rollback rehearsal.

    This class is intentionally observation-only.  It never edits product
    configuration, never enables Capture V2, and never performs a rollback.
    Release PASS is possible only when externally collected REAL_DUT evidence
    proves all three chronological phases.
    """

    GATE_ID = "R7-ROLLBACK-REHEARSAL"
    REQUIRED_PHASES = (
        RollbackPhase.PRE_V1,
        RollbackPhase.V2_ACTIVE,
        RollbackPhase.ROLLED_BACK_V1,
    )

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
        checks = (
            GateCheck(
                "pre_v1_authoritative",
                pre.capture_engine_version == "V1" and not pre.capture_v2_production_enabled,
                {"capture_engine_version": "V1", "capture_v2_production_enabled": False},
                {"capture_engine_version": pre.capture_engine_version,
                 "capture_v2_production_enabled": pre.capture_v2_production_enabled},
            ),
            GateCheck("pre_v1_healthy", pre.v1_healthy, True, pre.v1_healthy),
            GateCheck("pre_no_v2_producer", pre.v2_producer_count == 0, 0, pre.v2_producer_count),
            GateCheck(
                "v2_phase_observed",
                v2.capture_engine_version == "V2" and v2.capture_v2_production_enabled,
                {"capture_engine_version": "V2", "capture_v2_production_enabled": True},
                {"capture_engine_version": v2.capture_engine_version,
                 "capture_v2_production_enabled": v2.capture_v2_production_enabled},
            ),
            GateCheck("v2_producer_observed", v2.v2_producer_count >= 1, ">=1", v2.v2_producer_count),
            GateCheck(
                "rollback_v1_authoritative",
                rolled.capture_engine_version == "V1" and not rolled.capture_v2_production_enabled,
                {"capture_engine_version": "V1", "capture_v2_production_enabled": False},
                {"capture_engine_version": rolled.capture_engine_version,
                 "capture_v2_production_enabled": rolled.capture_v2_production_enabled},
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
                "Real-DUT evidence proves V2 -> V1 rollback and V1 health restoration."
                if verdict == GateVerdict.PASS
                else "Real-DUT rollback evidence contains failed state/health checks."
            ),
            facts={
                "reason": reason,
                "failed_checks": failed,
                "observations": [obs.as_dict() for obs in ordered],
            },
        )
