from __future__ import annotations

from pathlib import Path

import yaml

from app.actions.registry import ActionRegistry


REPO_ROOT = Path(__file__).resolve().parents[2]
PROFILE_ROOT = REPO_ROOT / "profiles"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "real-sip-registration-aba-live.yml"
TRIGGER_ACTION_ID = "TRIGGER_SIP_REGISTER_RC1"
EXPECTED_TRIGGER = "aim voip sip regc snd-reg RC1"


def test_registration_trigger_is_verified_repo_owned_action() -> None:
    action = ActionRegistry(PROFILE_ROOT).action(TRIGGER_ACTION_ID)

    assert action.contract_status == "VERIFIED"
    assert action.risk_level == "L1"
    assert action.executor == "shell"
    assert action.command == EXPECTED_TRIGGER
    assert action.supported_platforms == ["RUIJIE_VOIP_AIM_V1"]
    assert action.source_refs


def test_live_workflow_does_not_accept_runner_registration_trigger_command() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "SIP_ABA_REGISTRATION_TRIGGER_COMMAND" not in workflow
    assert "SIP_ABA_TRIGGER_ACTION_ID:" not in workflow
    assert f"readonly trigger_action_id='{TRIGGER_ACTION_ID}'" in workflow
    assert 'action = ActionRegistry(Path(os.environ["GITHUB_WORKSPACE"]) / "profiles").action(action_id)' in workflow
    assert 'action.contract_status != "VERIFIED"' in workflow
    assert 'action.risk_level not in {"L0", "L1"}' in workflow
    assert 'action.executor != "shell"' in workflow
    assert '"RUIJIE_VOIP_AIM_V1" not in action.supported_platforms' in workflow
    assert '--registration-trigger-command "$trigger_command"' in workflow


def test_live_trigger_command_has_no_yaml_indirection_or_multiline_payload() -> None:
    doc = yaml.safe_load((PROFILE_ROOT / "actions" / "voip_basic.yaml").read_text(encoding="utf-8"))
    rows = [row for row in doc["actions"] if row["id"] == TRIGGER_ACTION_ID]

    assert len(rows) == 1
    command = rows[0]["command"]
    assert command == EXPECTED_TRIGGER
    assert "\n" not in command
    assert "\r" not in command
    assert "${" not in command
    assert "`" not in command
