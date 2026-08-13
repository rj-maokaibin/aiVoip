from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from app.contracts.enums import ConfirmationPolicy, ExperimentVariant, ReproductionProfileStatus
from app.core.errors import AppError
from app.reproduction.profile import ReproductionProfileRegistry


def _canonical(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class ExperimentProfileDefinition(BaseModel):
    id: str
    name: str
    version: str = "1.0.0"
    status: ReproductionProfileStatus = ReproductionProfileStatus.ACTIVE
    hypothesis_codes: list[str] = Field(default_factory=list)
    reproduction_profile_id: str
    independent_variable: str
    target_finding: str
    confirmation_policy: ConfirmationPolicy
    sequence: list[ExperimentVariant]
    external_action_required: bool = True
    external_action_instructions: str = ""
    expected_change_paths: list[str] = Field(default_factory=list)
    must_equal_paths: list[str] = Field(default_factory=list)
    soft_drift_paths: list[str] = Field(default_factory=list)

    def canonical(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class LoadedExperimentProfile(BaseModel):
    definition: ExperimentProfileDefinition
    checksum: str
    source_file: str


class ExperimentProfileRegistry:
    def __init__(self, root: str | Path | None = None, reproduction_registry: ReproductionProfileRegistry | None = None):
        root = Path(root or Path(__file__).resolve().parents[3] / "profiles" / "experiments")
        self.root = root
        self.reproduction_registry = reproduction_registry or ReproductionProfileRegistry(root.parent)
        self._profiles: dict[str, LoadedExperimentProfile] = {}
        self.reload()

    def reload(self) -> None:
        result: dict[str, LoadedExperimentProfile] = {}
        for path in sorted(self.root.glob("*.yaml")):
            payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            for raw in payload.get("experiments") or []:
                d = ExperimentProfileDefinition.model_validate(raw)
                if d.id in result:
                    raise ValueError(f"DUPLICATE_EXPERIMENT_PROFILE:{d.id}")
                if not d.sequence:
                    raise ValueError(f"EXPERIMENT_SEQUENCE_EMPTY:{d.id}")
                if d.confirmation_policy in {ConfirmationPolicy.ABA_REQUIRED, ConfirmationPolicy.ABA_PREFERRED}:
                    required = {ExperimentVariant.A1, ExperimentVariant.B, ExperimentVariant.A2}
                    if not required.issubset(set(d.sequence)):
                        raise ValueError(f"EXPERIMENT_ABA_SEQUENCE_REQUIRED:{d.id}")
                if d.independent_variable not in d.expected_change_paths:
                    raise ValueError(f"EXPERIMENT_INDEPENDENT_VARIABLE_NOT_EXPECTED:{d.id}")
                overlap = set(d.expected_change_paths) & set(d.must_equal_paths)
                if overlap:
                    raise ValueError(f"EXPERIMENT_CONTROL_CONFLICT:{d.id}:{','.join(sorted(overlap))}")
                try:
                    self.reproduction_registry.get(d.reproduction_profile_id)
                except Exception as exc:
                    raise ValueError(f"EXPERIMENT_REPRODUCTION_PROFILE_MISSING:{d.id}:{d.reproduction_profile_id}") from exc
                checksum = hashlib.sha256(_canonical(d.canonical()).encode()).hexdigest()
                result[d.id] = LoadedExperimentProfile(definition=d, checksum=checksum, source_file=str(path))
        if not result:
            raise ValueError("EXPERIMENT_PROFILE_REGISTRY_EMPTY")
        self._profiles = result

    def get(self, profile_id: str) -> LoadedExperimentProfile:
        try:
            return self._profiles[profile_id]
        except KeyError as exc:
            raise AppError("EXPERIMENT_PROFILE_NOT_FOUND", details={"profile_id": profile_id}) from exc

    def list(self) -> list[LoadedExperimentProfile]:
        return [self._profiles[k] for k in sorted(self._profiles)]

    def candidates_for_hypothesis(self, hypothesis_code: str) -> list[LoadedExperimentProfile]:
        return [x for x in self.list() if hypothesis_code in x.definition.hypothesis_codes]
