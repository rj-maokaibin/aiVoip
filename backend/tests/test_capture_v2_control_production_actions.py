from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.capture_v2.control.policy import ControlPolicy, ControlPolicyError
from app.capture_v2.control.schema import ControlActionType, RemoteAction, SafetySpec


def _action(action_type: ControlActionType, *, parameters: dict | None = None) -> RemoteAction:
    now = datetime.now(timezone.utc)
    return RemoteAction(
        action_id=f"test-{action_type.value.lower()}",
        sequence=1,
        created_at=now,
        expires_at=now + timedelta(hours=1),
        action_type=action_type,
        parameters=parameters or {},
        safety=SafetySpec(expected_head="0" * 40),
    )


def _pretend_sudo_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    original = Path.is_file

    def patched(path: Path) -> bool:
        if path == Path("/usr/bin/sudo"):
            return True
        return original(path)

    monkeypatch.setattr(Path, "is_file", patched)


def test_explicit_production_preflight_uses_fixed_guarded_surface(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _pretend_sudo_exists(monkeypatch)
    policy = ControlPolicy(tmp_path)
    command = policy.prepare(
        _action(ControlActionType.PRODUCTION_DEPLOYMENT_PREFLIGHT, parameters={"timeout_seconds": 250})
    )

    assert command is not None
    assert command.timeout_seconds == 250
    assert command.argv[:5] == [
        "/usr/bin/sudo",
        "-n",
        __import__("sys").executable,
        "-m",
        "app.capture_v2.control.production_deployment_preflight_guarded",
    ]
    assert command.argv[-4:] == [
        "--repo-root",
        str(tmp_path.resolve()),
        "--authorization",
        str((tmp_path / "validation/capture_v2/PRODUCTION_CUTOVER_AUTHORIZATION_RC69.json").resolve()),
    ]


def test_explicit_production_cutover_uses_fixed_guarded_surface(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _pretend_sudo_exists(monkeypatch)
    policy = ControlPolicy(tmp_path)
    command = policy.prepare(
        _action(ControlActionType.PRODUCTION_CUTOVER, parameters={"timeout_seconds": 7100})
    )

    assert command is not None
    assert command.timeout_seconds == 7100
    assert command.argv[4] == "app.capture_v2.control.production_cutover_guarded"
    assert command.argv[-1].endswith("validation/capture_v2/PRODUCTION_CUTOVER_AUTHORIZATION_RC69.json")


@pytest.mark.parametrize(
    "action_type",
    [
        ControlActionType.PRODUCTION_DEPLOYMENT_PREFLIGHT,
        ControlActionType.PRODUCTION_CUTOVER,
    ],
)
def test_explicit_production_actions_reject_remote_path_injection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    action_type: ControlActionType,
) -> None:
    _pretend_sudo_exists(monkeypatch)
    policy = ControlPolicy(tmp_path)

    with pytest.raises(ControlPolicyError, match="PARAMETERS_NOT_ALLOWED"):
        policy.prepare(
            _action(
                action_type,
                parameters={
                    "timeout_seconds": 300,
                    "authorization": "/tmp/attacker.json",
                    "env_path": "/tmp/attacker.env",
                },
            )
        )


def test_remote_action_schema_accepts_explicit_production_types() -> None:
    now = datetime.now(timezone.utc)
    for action_type in (
        ControlActionType.PRODUCTION_DEPLOYMENT_PREFLIGHT,
        ControlActionType.PRODUCTION_CUTOVER,
    ):
        parsed = RemoteAction.from_dict(
            {
                "schema_version": "capture-v2-remote-action-v1",
                "action_id": f"schema-{action_type.value.lower()}",
                "sequence": 1,
                "created_at": now.isoformat(),
                "expires_at": (now + timedelta(hours=1)).isoformat(),
                "action_type": action_type.value,
                "parameters": {"timeout_seconds": 300},
                "safety": {"expected_head": "0" * 40},
            }
        )
        assert parsed.action_type is action_type
