import pytest

from app.diagnosis.ai_runtime import AIPromotionStage, AIRuntimePolicy
from app.diagnosis.controlled_ai_selection import (
    ControlledAISelectionError,
    resolve_registered_selection,
)


def _proposal():
    return {
        "next_question_key": "DTMF_FIRST_MISMATCH_LAYER",
        "recommended_action": {
            "action_type": "RECOMMEND_QUESTION",
            "question_key": "DTMF_FIRST_MISMATCH_LAYER",
            "reason": "distinguish PCM_RX from AIM number path",
        },
    }


def test_suggest_stage_can_surface_registered_selection_but_not_dispatch():
    selection = resolve_registered_selection(
        _proposal(),
        runtime=AIRuntimePolicy(AIPromotionStage.SUGGEST, promotion_gate_passed=False),
        source_proposal_id="p1",
    )
    assert selection.registered_id == "DTMF_FIRST_MISMATCH_LAYER"
    assert selection.dispatch_allowed is False
    assert selection.raw_command_allowed is False


def test_controlled_planner_dispatch_directive_requires_promotion_gate():
    with pytest.raises(ControlledAISelectionError, match="AI_PROMOTION_GATE_REQUIRED"):
        resolve_registered_selection(
            _proposal(),
            runtime=AIRuntimePolicy(AIPromotionStage.CONTROLLED_PLANNER, promotion_gate_passed=False),
            require_dispatch_authority=True,
        )
    selection = resolve_registered_selection(
        _proposal(),
        runtime=AIRuntimePolicy(AIPromotionStage.CONTROLLED_PLANNER, promotion_gate_passed=True),
        require_dispatch_authority=True,
    )
    assert selection.dispatch_allowed is True
    assert selection.raw_command_allowed is False
