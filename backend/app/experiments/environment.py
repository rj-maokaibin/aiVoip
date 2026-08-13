from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.contracts.enums import DriftClassification, EnvironmentComparisonStatus, EventType
from app.db.models import (
    CaseDevice, DiagnosticExperiment, EnvironmentComparison, ExperimentEnvironmentSnapshot,
    ExperimentRun, ReproductionCall, ReproductionSession,
)
from app.experiments.profile import ExperimentProfileDefinition
from app.services.events import emit_event


def _canonical(data: dict[str, Any]) -> bytes:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _path_get(data: dict[str, Any], path: str) -> tuple[bool, Any]:
    current: Any = data
    for token in path.split("."):
        if not isinstance(current, dict) or token not in current:
            return False, None
        current = current[token]
    return True, current


def _deep_merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


class EnvironmentSnapshotBuilder:
    """Builds an immutable experiment environment snapshot from structured runtime data.

    It never executes DUT commands. EC-02 can later populate the same fields from a real
    PlatformProfile without changing comparator semantics.
    """

    version = "1.0.0"

    def build(
        self,
        db: Session,
        *,
        experiment: DiagnosticExperiment,
        run: ExperimentRun,
        external_state: dict[str, Any] | None = None,
        call_context: dict[str, Any] | None = None,
        phase: str = "PRE",
        overrides: dict[str, Any] | None = None,
    ) -> ExperimentEnvironmentSnapshot:
        session = db.get(ReproductionSession, run.reproduction_session_id) if run.reproduction_session_id else None
        call = db.get(ReproductionCall, run.reproduction_call_id) if run.reproduction_call_id else None
        device = db.get(CaseDevice, session.device_id) if session else None
        info = dict((device.device_info or {}) if device else {})
        voice = dict((session.voice_runtime_context_json or {}) if session else {})
        quick = dict((call.quick_analysis_json or {}) if call else {})
        metrics = dict(quick.get("metrics") or {})
        ctx = dict(call_context or {})
        payload = {
            "schema_version": 1,
            "builder_version": self.version,
            "device": {
                "serial": (device.sn if device else info.get("serial")),
                "model": info.get("model") or info.get("product_model"),
            },
            "software": {
                "version": info.get("software_version") or info.get("version"),
            },
            "boot": {
                "boot_id": info.get("boot_id"),
                "uptime_seconds": info.get("uptime_seconds"),
            },
            "voice": {
                "voice_vlan_id": voice.get("voice_vlan_id"),
                "interface": voice.get("voice_interface"),
                "gateway_ip": voice.get("voice_gateway_ip"),
                "device_ip": voice.get("voice_device_ip"),
                "fxs_port": ctx.get("fxs_port") or metrics.get("fxs_port") or info.get("fxs_port"),
            },
            "call": {
                "codec": ctx.get("codec") or metrics.get("codec"),
                "ptime": ctx.get("ptime") or metrics.get("ptime"),
                "direction": ctx.get("direction") or metrics.get("direction"),
                "remote_endpoint": ctx.get("remote_endpoint") or metrics.get("remote_endpoint"),
                "called_number": ctx.get("called_number") or metrics.get("called_number"),
            },
            "reproduction": {
                "profile_id": (session.profile_key if session else experiment.reproduction_profile_id),
                "profile_version": (session.profile_version if session else None),
                "capture_stage": (session.capture_stage if session else None),
            },
            "external": dict(external_state or {}),
        }
        if overrides:
            payload = _deep_merge(payload, overrides)
        checksum = hashlib.sha256(_canonical(payload)).hexdigest()
        row = ExperimentEnvironmentSnapshot(
            experiment_id=experiment.id,
            run_id=run.id,
            case_id=experiment.case_id,
            phase=phase,
            snapshot_json=payload,
            checksum=checksum,
        )
        db.add(row)
        db.flush()
        return row


@dataclass(frozen=True)
class EnvironmentComparisonDecision:
    status: EnvironmentComparisonStatus
    expected_changes: tuple[dict[str, Any], ...]
    soft_drift: tuple[dict[str, Any], ...]
    hard_drift: tuple[dict[str, Any], ...]
    compared_fields: dict[str, Any]

    @property
    def comparable(self) -> bool:
        return self.status != EnvironmentComparisonStatus.NOT_COMPARABLE


class EnvironmentComparator:
    version = "1.0.0"

    def evaluate(
        self,
        *,
        profile: ExperimentProfileDefinition,
        baseline: dict[str, Any],
        variant: dict[str, Any],
        revert: bool = False,
    ) -> EnvironmentComparisonDecision:
        expected: list[dict[str, Any]] = []
        soft: list[dict[str, Any]] = []
        hard: list[dict[str, Any]] = []
        compared: dict[str, Any] = {}

        for path in profile.expected_change_paths:
            a_ok, a = _path_get(baseline, path)
            b_ok, b = _path_get(variant, path)
            compared[path] = {"baseline": a, "variant": b, "available": a_ok and b_ok}
            if not (a_ok and b_ok):
                hard.append({"path": path, "classification": DriftClassification.HARD_DRIFT.value, "reason": "EXPECTED_FIELD_UNAVAILABLE", "baseline": a, "variant": b})
                continue
            if revert:
                if a != b:
                    hard.append({"path": path, "classification": DriftClassification.HARD_DRIFT.value, "reason": "EXPECTED_REVERT_NOT_OBSERVED", "baseline": a, "variant": b})
                else:
                    expected.append({"path": path, "classification": DriftClassification.EXPECTED_CHANGE.value, "reason": "REVERT_CONFIRMED", "baseline": a, "variant": b})
            else:
                if a == b:
                    hard.append({"path": path, "classification": DriftClassification.HARD_DRIFT.value, "reason": "EXPECTED_CHANGE_NOT_OBSERVED", "baseline": a, "variant": b})
                else:
                    expected.append({"path": path, "classification": DriftClassification.EXPECTED_CHANGE.value, "baseline": a, "variant": b})

        for path in profile.must_equal_paths:
            a_ok, a = _path_get(baseline, path)
            b_ok, b = _path_get(variant, path)
            compared[path] = {"baseline": a, "variant": b, "available": a_ok and b_ok}
            if not (a_ok and b_ok):
                hard.append({"path": path, "classification": DriftClassification.HARD_DRIFT.value, "reason": "CONTROL_FIELD_UNAVAILABLE", "baseline": a, "variant": b})
            elif a != b:
                hard.append({"path": path, "classification": DriftClassification.HARD_DRIFT.value, "reason": "CONTROL_VARIABLE_CHANGED", "baseline": a, "variant": b})

        for path in profile.soft_drift_paths:
            a_ok, a = _path_get(baseline, path)
            b_ok, b = _path_get(variant, path)
            compared[path] = {"baseline": a, "variant": b, "available": a_ok and b_ok}
            if a_ok and b_ok and a != b:
                soft.append({"path": path, "classification": DriftClassification.SOFT_DRIFT.value, "baseline": a, "variant": b})

        status = (
            EnvironmentComparisonStatus.NOT_COMPARABLE
            if hard
            else EnvironmentComparisonStatus.COMPARABLE_WITH_SOFT_DRIFT
            if soft
            else EnvironmentComparisonStatus.COMPARABLE
        )
        return EnvironmentComparisonDecision(status, tuple(expected), tuple(soft), tuple(hard), compared)

    def compare_and_persist(
        self,
        db: Session,
        *,
        experiment: DiagnosticExperiment,
        profile: ExperimentProfileDefinition,
        baseline_run: ExperimentRun,
        variant_run: ExperimentRun,
        baseline_snapshot: ExperimentEnvironmentSnapshot,
        variant_snapshot: ExperimentEnvironmentSnapshot,
        revert: bool = False,
    ) -> EnvironmentComparison:
        decision = self.evaluate(
            profile=profile,
            baseline=baseline_snapshot.snapshot_json,
            variant=variant_snapshot.snapshot_json,
            revert=revert,
        )
        row = EnvironmentComparison(
            experiment_id=experiment.id,
            baseline_run_id=baseline_run.id,
            variant_run_id=variant_run.id,
            status=decision.status.value,
            expected_changes_json=list(decision.expected_changes),
            soft_drift_json=list(decision.soft_drift),
            hard_drift_json=list(decision.hard_drift),
            compared_fields_json=decision.compared_fields,
        )
        db.add(row)
        db.flush()
        emit_event(
            db,
            event_type=EventType.ENVIRONMENT_COMPARISON_COMPLETED,
            case_id=experiment.case_id,
            entity_type="environment_comparison",
            entity_id=row.id,
            payload={"experiment_id": experiment.id, "status": row.status, "hard_drift": row.hard_drift_json or [], "soft_drift": row.soft_drift_json or []},
        )
        return row
