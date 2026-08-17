import json
from types import SimpleNamespace

from app.diagnosis.ai_runtime import AICapability, AIPromotionStage, AIRuntimePolicy


def _settings(tmp_path, *, env="production", manual=False, allow_manual=False, artifact_payload=None):
    artifact = tmp_path / "ai_promotion_gate.json"
    if artifact_payload is not None:
        artifact.write_text(json.dumps(artifact_payload), encoding="utf-8")
    return SimpleNamespace(
        ai_promotion_stage="CONTROLLED_PLANNER",
        ai_promotion_gate_artifact=artifact,
        ai_promotion_gate_passed=manual,
        ai_allow_manual_promotion_override=allow_manual,
        app_env=env,
    )


def _passing_artifact():
    return {
        "schema_version": "ai-promotion-gate-v1",
        "status": "PASS",
        "promotion_stage_allowed": "CONTROLLED_PLANNER",
        "formal_reasoner_authority": "DETERMINISTIC_ONLY",
        "raw_device_command_authority": "FORBIDDEN",
        "ai_only_root_cause_confirmation": "FORBIDDEN",
    }


def test_production_ignores_manual_boolean_without_gate_artifact(tmp_path):
    policy = AIRuntimePolicy.from_settings(
        _settings(tmp_path, env="production", manual=True, allow_manual=True)
    )
    assert policy.stage is AIPromotionStage.CONTROLLED_PLANNER
    assert policy.promotion_gate_passed is False
    assert policy.enabled(AICapability.REGISTERED_PLAN_SELECTION) is False
    assert policy.gate_source == "NONE"


def test_passing_gate_artifact_enables_only_registered_selection(tmp_path):
    policy = AIRuntimePolicy.from_settings(
        _settings(tmp_path, artifact_payload=_passing_artifact())
    )
    assert policy.promotion_gate_passed is True
    assert policy.gate_source == "ARTIFACT"
    assert policy.enabled(AICapability.REGISTERED_PLAN_SELECTION) is True
    assert policy.formal_reasoner_may_use_ai is False
    assert policy.may_execute_device_command is False


def test_development_manual_override_requires_explicit_allow_flag(tmp_path):
    denied = AIRuntimePolicy.from_settings(
        _settings(tmp_path, env="development", manual=True, allow_manual=False)
    )
    allowed = AIRuntimePolicy.from_settings(
        _settings(tmp_path, env="development", manual=True, allow_manual=True)
    )
    assert denied.promotion_gate_passed is False
    assert allowed.promotion_gate_passed is True
    assert allowed.gate_source == "DEV_MANUAL_OVERRIDE"
