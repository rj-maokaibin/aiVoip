from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class EvidenceV2Rollout:
    compose: bool
    project: bool
    strict_validator: bool
    mode: str

    @property
    def identity_token(self) -> str:
        return f"evidence-v2:{self.mode}:strict={int(self.strict_validator)}"


def rollout_from_env(env: dict[str, str] | None = None) -> EvidenceV2Rollout:
    values = env if env is not None else os.environ
    compose = _bool(values.get("PRELIMINARY_EVIDENCE_V2_COMPOSE"), default=False)
    project = _bool(values.get("PRELIMINARY_EVIDENCE_V2_PROJECT"), default=False)
    strict = _bool(values.get("PRELIMINARY_EVIDENCE_V2_STRICT_VALIDATOR"), default=True)

    if project and not compose:
        raise ValueError("EVIDENCE_V2_PROJECT_REQUIRES_COMPOSE")
    mode = "V1"
    if compose and not project:
        mode = "SHADOW"
    elif compose and project:
        mode = "V2"
    return EvidenceV2Rollout(compose=compose, project=project, strict_validator=strict, mode=mode)


def report_idempotency_mode_token(*, v1_schema: str, v1_composer: str, rollout: EvidenceV2Rollout) -> str:
    """Ensure changing rollout mode cannot replay an old V1 report as V2."""

    return f"{v1_schema}|{v1_composer}|{rollout.identity_token}"


def _bool(value: str | bool | None, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"INVALID_BOOLEAN:{value}")
