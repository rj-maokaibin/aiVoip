from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class AIPromotionStage(str, Enum):
    OFF = "OFF"
    SHADOW = "SHADOW"
    SUGGEST = "SUGGEST"
    CONTROLLED_PLANNER = "CONTROLLED_PLANNER"


class AICapability(str, Enum):
    HYPOTHESIS = "HYPOTHESIS"
    CRITIC = "CRITIC"
    PLANNER = "PLANNER"
    EXPLANATION = "EXPLANATION"
    REGISTERED_PLAN_SELECTION = "REGISTERED_PLAN_SELECTION"


_STAGE_CAPABILITIES: dict[AIPromotionStage, frozenset[AICapability]] = {
    AIPromotionStage.OFF: frozenset(),
    AIPromotionStage.SHADOW: frozenset({
        AICapability.HYPOTHESIS,
        AICapability.CRITIC,
        AICapability.EXPLANATION,
    }),
    AIPromotionStage.SUGGEST: frozenset({
        AICapability.HYPOTHESIS,
        AICapability.CRITIC,
        AICapability.PLANNER,
        AICapability.EXPLANATION,
    }),
    AIPromotionStage.CONTROLLED_PLANNER: frozenset({
        AICapability.HYPOTHESIS,
        AICapability.CRITIC,
        AICapability.PLANNER,
        AICapability.EXPLANATION,
        AICapability.REGISTERED_PLAN_SELECTION,
    }),
}


def _gate_artifact_passed(path: str | Path | None) -> bool:
    if not path:
        return False
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False
    return bool(
        payload.get("schema_version") == "ai-promotion-gate-v1"
        and payload.get("status") == "PASS"
        and payload.get("promotion_stage_allowed") == "CONTROLLED_PLANNER"
        and payload.get("formal_reasoner_authority") == "DETERMINISTIC_ONLY"
        and payload.get("raw_device_command_authority") == "FORBIDDEN"
        and payload.get("ai_only_root_cause_confirmation") == "FORBIDDEN"
    )


@dataclass(frozen=True)
class AIRuntimePolicy:
    """Capability-specific promotion policy for the LLM sidecar.

    The deterministic diagnosis engine remains the only formal diagnosis authority at
    every promotion stage. CONTROLLED_PLANNER means AI may select *registered*
    question/profile/experiment identifiers only after a persisted promotion artifact
    proves the real-model quality gate passed. It still cannot emit or execute raw
    shell/device commands and cannot confirm a root cause by itself.
    """

    stage: AIPromotionStage = AIPromotionStage.OFF
    promotion_gate_passed: bool = False
    gate_source: str = "NONE"

    @classmethod
    def from_settings(cls, settings) -> "AIRuntimePolicy":
        raw = str(getattr(settings, "ai_promotion_stage", "OFF") or "OFF").upper()
        try:
            stage = AIPromotionStage(raw)
        except ValueError:
            stage = AIPromotionStage.OFF

        artifact = getattr(settings, "ai_promotion_gate_artifact", "")
        artifact_passed = _gate_artifact_passed(artifact)
        manual_requested = bool(getattr(settings, "ai_promotion_gate_passed", False))
        manual_allowed = bool(getattr(settings, "ai_allow_manual_promotion_override", False))
        environment = str(getattr(settings, "app_env", "development") or "development").lower()
        # Production can never trust a boolean environment flag for promotion. A
        # machine-generated gate artifact is mandatory. Development/test may opt in
        # to an explicit manual override for local contract tests only.
        manual_effective = manual_requested and manual_allowed and environment not in {"production", "prod"}
        passed = artifact_passed or manual_effective
        source = "ARTIFACT" if artifact_passed else ("DEV_MANUAL_OVERRIDE" if manual_effective else "NONE")
        return cls(stage=stage, promotion_gate_passed=passed, gate_source=source)

    def enabled(self, capability: AICapability) -> bool:
        if capability not in _STAGE_CAPABILITIES[self.stage]:
            return False
        if capability is AICapability.REGISTERED_PLAN_SELECTION:
            return self.promotion_gate_passed
        return True

    @property
    def run_gateway(self) -> bool:
        return self.stage is not AIPromotionStage.OFF

    @property
    def formal_reasoner_may_use_ai(self) -> bool:
        return False

    @property
    def may_execute_device_command(self) -> bool:
        return False

    def describe(self) -> dict:
        return {
            "stage": self.stage.value,
            "promotion_gate_passed": self.promotion_gate_passed,
            "gate_source": self.gate_source,
            "capabilities": sorted(
                capability.value
                for capability in _STAGE_CAPABILITIES[self.stage]
                if self.enabled(capability)
            ),
            "formal_reasoner_may_use_ai": False,
            "may_execute_device_command": False,
        }
