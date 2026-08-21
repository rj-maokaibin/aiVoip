import subprocess
from pathlib import Path

import pytest

from app.capture_v2.control.policy import ControlPolicy, ControlPolicyError
from app.capture_v2.control.schema import RemoteAction


def _repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "gate@example.invalid"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Gate Test"], cwd=tmp_path, check=True)
    (tmp_path / "backend/tests").mkdir(parents=True)
    (tmp_path / "backend/tests/test_capture_v2_x.py").write_text("def test_x(): assert True\n")
    subprocess.run(["git", "add", "backend/tests/test_capture_v2_x.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=tmp_path, check=True)
    return tmp_path


def _head(repo):
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()


def _action(action_type, params, safety=None):
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    return RemoteAction.from_dict({
        "schema_version": "capture-v2-remote-action-v1", "action_id": "A-1", "sequence": 1,
        "created_at": now.isoformat(), "expires_at": (now + timedelta(minutes=5)).isoformat(),
        "action_type": action_type, "parameters": params, "safety": safety or {},
    })


def test_policy_has_no_arbitrary_shell_and_builds_fixed_gate_cli(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setenv("CAPTURE_ENGINE_VERSION", "V1")
    monkeypatch.setenv("CAPTURE_V2_PRODUCTION_ENABLED", "false")
    p = ControlPolicy(repo)
    action = _action("GATE_EVALUATE", {"bundle": "/tmp/b", "gate_id": "R3-08"}, {"expected_head": _head(repo)})
    p.check_safety(action)
    cmd = p.prepare(action)
    assert cmd.argv[1:4] == ["-m", "app.capture_v2.gate_cli", "evaluate"]


def test_policy_rejects_v2_production_enabled(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setenv("CAPTURE_ENGINE_VERSION", "V1")
    monkeypatch.setenv("CAPTURE_V2_PRODUCTION_ENABLED", "true")
    with pytest.raises(ControlPolicyError, match="V2_PRODUCTION"):
        ControlPolicy(repo).check_safety(_action("GATE_EVALUATE", {"bundle": "/tmp/b", "gate_id": "R3"}, {"expected_head": _head(repo)}))


def test_policy_requires_explicit_kill_approval(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setenv("CAPTURE_ENGINE_VERSION", "V1")
    monkeypatch.setenv("CAPTURE_V2_PRODUCTION_ENABLED", "false")
    action = _action("FAULT_WORKER_SIGNAL", {"pid": 123, "signal": "kill", "confirm_owned_worker": True}, {"expected_head": _head(repo)})
    with pytest.raises(ControlPolicyError, match="WORKER_KILL_NOT_APPROVED"):
        ControlPolicy(repo).prepare(action)


def test_policy_rejects_dirty_tracked_code(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setenv("CAPTURE_ENGINE_VERSION", "V1")
    monkeypatch.setenv("CAPTURE_V2_PRODUCTION_ENABLED", "false")
    (repo / "backend/tests/test_capture_v2_x.py").write_text("def test_x(): assert False\n")
    action = _action("GATE_EVALUATE", {"bundle": "/tmp/b", "gate_id": "R3"}, {"expected_head": _head(repo)})
    with pytest.raises(ControlPolicyError, match="SAFETY_GIT_DIRTY"):
        ControlPolicy(repo).check_safety(action)


def test_policy_requires_expected_head(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setenv("CAPTURE_ENGINE_VERSION", "V1")
    monkeypatch.setenv("CAPTURE_V2_PRODUCTION_ENABLED", "false")
    action = _action("GATE_EVALUATE", {"bundle": "/tmp/b", "gate_id": "R3"})
    with pytest.raises(ControlPolicyError, match="EXPECTED_HEAD_REQUIRED"):
        ControlPolicy(repo).check_safety(action)
