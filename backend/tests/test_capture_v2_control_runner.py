import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.capture_v2.control.runner import RemoteValidationRunner
from app.capture_v2.control.schema import ControlState


def _repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "gate@example.invalid"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Gate Test"], cwd=tmp_path, check=True)
    (tmp_path / "backend/tests").mkdir(parents=True)
    (tmp_path / "backend/tests/test_capture_v2_smoke.py").write_text("def test_smoke(): assert True\n")
    subprocess.run(["git", "add", "backend/tests/test_capture_v2_smoke.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=tmp_path, check=True)
    (tmp_path / "validation/control").mkdir(parents=True)
    return tmp_path


def _write_action(repo: Path, *, action_id="H-1", sequence=1, token="ACK1"):
    now = datetime.now(timezone.utc)
    payload = {
        "schema_version": "capture-v2-remote-action-v1", "action_id": action_id, "sequence": sequence,
        "created_at": now.isoformat(), "expires_at": (now + timedelta(minutes=5)).isoformat(),
        "action_type": "HUMAN_STEP",
        "parameters": {"instruction": "Pick up handset and dial 1001", "ack_token": token},
        "safety": {"require_clean_git": True,
            "expected_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()},
    }
    (repo / "validation/control/next_action.json").write_text(json.dumps(payload))


def test_human_step_waits_then_ack_succeeds(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setenv("CAPTURE_ENGINE_VERSION", "V1")
    monkeypatch.setenv("CAPTURE_V2_PRODUCTION_ENABLED", "false")
    _write_action(repo)
    r = RemoteValidationRunner(repo_root=repo, runner_id="test-runner")
    assert r.process_once().state == ControlState.WAITING_HUMAN
    (repo / "validation/control/human_ack.json").write_text(json.dumps({"action_id": "H-1", "token": "ACK1"}))
    assert r.process_once().state == ControlState.SUCCEEDED


def test_action_id_reuse_with_changed_payload_is_rejected(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setenv("CAPTURE_ENGINE_VERSION", "V1")
    monkeypatch.setenv("CAPTURE_V2_PRODUCTION_ENABLED", "false")
    _write_action(repo)
    r = RemoteValidationRunner(repo_root=repo, runner_id="test-runner")
    assert r.process_once().state == ControlState.WAITING_HUMAN
    (repo / "validation/control/human_ack.json").write_text(json.dumps({"action_id": "H-1", "token": "ACK1"}))
    assert r.process_once().state == ControlState.SUCCEEDED
    _write_action(repo, token="DIFFERENT")
    assert r.process_once().state == ControlState.REJECTED


def test_sequence_must_be_monotonic(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setenv("CAPTURE_ENGINE_VERSION", "V1")
    monkeypatch.setenv("CAPTURE_V2_PRODUCTION_ENABLED", "false")
    _write_action(repo, action_id="H-1", sequence=2)
    (repo / "validation/control/human_ack.json").write_text(json.dumps({"action_id": "H-1", "token": "ACK1"}))
    r = RemoteValidationRunner(repo_root=repo, runner_id="test-runner")
    assert r.process_once().state == ControlState.SUCCEEDED
    _write_action(repo, action_id="H-2", sequence=1)
    assert r.process_once().state == ControlState.REJECTED


def test_control_source_update_requests_reexec(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    r = RemoteValidationRunner(repo_root=repo, runner_id="test-runner")
    r._loaded_head = "old-head"
    calls = []
    monkeypatch.setattr(r, "_control_source_changed", lambda old, new: (old, new) == ("old-head", "new-head"))
    monkeypatch.setattr(r, "_reexec_current_process", lambda: calls.append("reexec"))

    r._maybe_reexec_after_sync("new-head")

    assert calls == ["reexec"]
    assert r._loaded_head == "new-head"


def test_reexec_preserves_module_launch_semantics(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    r = RemoteValidationRunner(repo_root=repo, runner_id="test-runner")
    original_argv = [
        "/repo/backend/app/capture_v2/control_cli.py",
        "run",
        "--repo-root",
        "..",
        "--git-sync",
    ]
    calls = []
    monkeypatch.setattr(sys, "argv", original_argv)
    monkeypatch.setattr(os, "execv", lambda executable, argv: calls.append((executable, argv)))

    r._reexec_current_process()

    assert calls == [(
        sys.executable,
        [sys.executable, "-m", "app.capture_v2.control_cli", *original_argv[1:]],
    )]


def test_only_control_python_changes_require_reexec(tmp_path):
    repo = _repo(tmp_path)
    r = RemoteValidationRunner(repo_root=repo, runner_id="test-runner")
    old_head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()

    (repo / "validation/control/next_action.json").write_text("{}\n")
    subprocess.run(["git", "add", "validation/control/next_action.json"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "control action"], cwd=repo, check=True)
    action_head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    assert r._control_source_changed(old_head, action_head) is False

    (repo / "backend/app/capture_v2/control").mkdir(parents=True)
    (repo / "backend/app/capture_v2/control/schema.py").write_text("# changed control schema\n")
    subprocess.run(["git", "add", "backend/app/capture_v2/control/schema.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "control source"], cwd=repo, check=True)
    source_head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    assert r._control_source_changed(action_head, source_head) is True


def test_runner_error_is_published_once_per_distinct_error(tmp_path):
    repo = _repo(tmp_path)
    r = RemoteValidationRunner(repo_root=repo, runner_id="test-runner")
    pushes = []

    class _Sync:
        def commit_and_push(self, paths, *, message):
            pushes.append((tuple(str(path) for path in paths), message))
            return "deadbeef"

    r.sync = _Sync()
    r._publish_runner_error(ValueError("ACTION_TYPE_UNSUPPORTED"))
    r._publish_runner_error(ValueError("ACTION_TYPE_UNSUPPORTED"))

    status = json.loads((repo / "validation/control/status.json").read_text())
    assert status["state"] == "RUNNER_ERROR"
    assert status["error"] == "ValueError:ACTION_TYPE_UNSUPPORTED"
    assert len(pushes) == 1
