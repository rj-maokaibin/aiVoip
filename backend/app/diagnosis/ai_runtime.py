from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


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


@dataclass(frozen=True)
class AIRuntimePolicy:
    """Capability-specific promotion policy for the LLM sidecar.

    The deterministic diagnosis engine remains the only formal diagnosis authority at
    every promotion stage.  CONTROLLED_PLANNER means the AI may select *registered*
    question/profile/experiment identifiers after an external promotion gate passes;
    it still cannot emit or execute shell/device commands and cannot confirm a root
    cause by itself.
    """

    stage: AIPromotionStage = AIPromotionStage.OFF
    promotion_gate_passed: bool = False

    @classmethod
    def from_settings(cls, settings) -> "AIRuntimePolicy":
        raw = str(getattr(settings, "ai_promotion_stage", "OFF") or "OFF").upper()
        try:
            stage = AIPromotionStage(raw)
        except ValueError:
            stage = AIPromotionStage.OFF
        return cls(
            stage=stage,
            promotion_gate_passed=bool(getattr(settings, "ai_promotion_gate_passed", False)),
        )

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
        # Deliberate invariant: model output is never a DiagnosisDecision authority.
        return False

    @property
    def may_execute_device_command(self) -> bool:
        return False

    def describe(self) -> dict:
        return {
            "stage": self.stage.value,
            "promotion_gate_passed": self.promotion_gate_passed,
            "capabilities": sorted(
                capability.value
                for capability in _STAGE_CAPABILITIES[self.stage]
                if self.enabled(capability)
            ),
            "formal_reasoner_may_use_ai": False,
            "may_execute_device_command": False,
        }
