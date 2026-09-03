import pytest

from app.reports.v2.migration import report_idempotency_mode_token, rollout_from_env


def test_rollout_defaults_to_v1_with_strict_validator_ready():
    rollout = rollout_from_env({})
    assert rollout.mode == "V1"
    assert rollout.compose is False
    assert rollout.project is False
    assert rollout.strict_validator is True


def test_shadow_and_v2_projection_modes_are_explicit():
    shadow = rollout_from_env({"PRELIMINARY_EVIDENCE_V2_COMPOSE": "true"})
    assert shadow.mode == "SHADOW"
    v2 = rollout_from_env({
        "PRELIMINARY_EVIDENCE_V2_COMPOSE": "true",
        "PRELIMINARY_EVIDENCE_V2_PROJECT": "true",
    })
    assert v2.mode == "V2"


def test_projection_without_compose_is_rejected():
    with pytest.raises(ValueError, match="EVIDENCE_V2_PROJECT_REQUIRES_COMPOSE"):
        rollout_from_env({"PRELIMINARY_EVIDENCE_V2_PROJECT": "true"})


def test_idempotency_identity_changes_when_rollout_changes():
    v1 = rollout_from_env({})
    shadow = rollout_from_env({"PRELIMINARY_EVIDENCE_V2_COMPOSE": "true"})
    assert report_idempotency_mode_token(v1_schema="v1", v1_composer="c1", rollout=v1) != \
           report_idempotency_mode_token(v1_schema="v1", v1_composer="c1", rollout=shadow)
