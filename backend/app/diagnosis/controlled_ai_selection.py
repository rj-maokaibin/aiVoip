from __future__ import annotations

from dataclasses import dataclass

from app.diagnosis.ai_runtime import AICapability, AIRuntimePolicy
from app.experiments.profile import ExperimentProfileRegistry
from app.reproduction.profile import ReproductionProfileRegistry
from app.reproduction.question_graph import DiagnosticQuestionRegistry


@dataclass(frozen=True)
class ControlledAISelection:
    kind: str
    registered_id: str
    reason: str
    source_proposal_id: str | None
    dispatch_allowed: bool
    raw_command_allowed: bool = False

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "registered_id": self.registered_id,
            "reason": self.reason,
            "source_proposal_id": self.source_proposal_id,
            "dispatch_allowed": self.dispatch_allowed,
            "raw_command_allowed": False,
            "formal_diagnosis_authority": False,
        }


class ControlledAISelectionError(RuntimeError):
    pass


def resolve_registered_selection(
    proposal: dict,
    *,
    runtime: AIRuntimePolicy,
    source_proposal_id: str | None = None,
    require_dispatch_authority: bool = False,
) -> ControlledAISelection | None:
    """Resolve an AI recommendation into a registry-backed selection directive.

    This is the only bridge intended for a future orchestrator integration. It does
    not execute anything. The caller must still use the existing deterministic
    reproduction/experiment service, which revalidates profile/action contracts.
    """
    action = proposal.get("recommended_action") or {}
    action_type = str(action.get("action_type") or "")
    if not action_type:
        return None

    if not runtime.enabled(AICapability.PLANNER):
        raise ControlledAISelectionError("AI_PLANNER_CAPABILITY_DISABLED")

    kind = ""
    registered_id = ""
    if action_type == "RECOMMEND_QUESTION":
        kind = "QUESTION"
        registered_id = str(action.get("question_key") or proposal.get("next_question_key") or "")
        DiagnosticQuestionRegistry().get(registered_id)
    elif action_type == "RECOMMEND_REPRODUCTION_PROFILE":
        kind = "REPRODUCTION_PROFILE"
        registered_id = str(action.get("profile_id") or "")
        ReproductionProfileRegistry().get(registered_id)
    elif action_type == "RECOMMEND_EXPERIMENT_PROFILE":
        kind = "EXPERIMENT_PROFILE"
        registered_id = str(action.get("experiment_profile_id") or "")
        ExperimentProfileRegistry().get(registered_id)
    elif action_type == "REQUEST_USER_EVIDENCE":
        # No automatic device action exists for free-form user evidence requests.
        kind = "USER_EVIDENCE_REQUEST"
        registered_id = "USER_EVIDENCE_REQUEST"
    else:
        raise ControlledAISelectionError(f"AI_ACTION_NOT_ALLOWED:{action_type}")

    dispatch_allowed = runtime.enabled(AICapability.REGISTERED_PLAN_SELECTION)
    if require_dispatch_authority and not dispatch_allowed:
        raise ControlledAISelectionError("AI_PROMOTION_GATE_REQUIRED")

    return ControlledAISelection(
        kind=kind,
        registered_id=registered_id,
        reason=str(action.get("reason") or "AI registered recommendation"),
        source_proposal_id=source_proposal_id,
        dispatch_allowed=dispatch_allowed,
    )
