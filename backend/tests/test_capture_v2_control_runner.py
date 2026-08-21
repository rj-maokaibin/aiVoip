import json
import subprocess
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
