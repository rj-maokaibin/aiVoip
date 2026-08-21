from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.capture_v2.control.policy import ControlPolicy
from app.capture_v2.control.schema import ControlActionType, RemoteAction, SafetySpec


def test_r1_fencing_action_is_registered():
    assert ControlActionType.GATE_LEASE_FENCING.value == "GATE_LEASE_FENCING"


def test_r1_fencing_policy_builds_allowlisted_module_command(tmp_path: Path):
    backend = tmp_path / "backend"
    backend.mkdir()
    action = RemoteAction(
        action_id="R1-RC2-002",
        sequence=3,
        created_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        action_type=ControlActionType.GATE_LEASE_FENCING,
        parameters={
            "device_id": "dev-1",
            "capture_session_a": "session-a",
            "capture_session_b": "session-b",
            "worker_a": "a",
            "worker_b": "b",
            "ttl_seconds": 10,
            "gate_id": "R1-02-RC2",
        },
        safety=SafetySpec(expected_head="deadbeef"),
    )
    prepared = ControlPolicy(tmp_path).prepare(action)
    assert prepared is not None
    assert prepared.cwd == backend
    assert prepared.argv[1:3] == ["-m", "app.capture_v2.control.r1_fencing_gate"]
    assert "--device-id" in prepared.argv
    assert "--ttl-seconds" in prepared.argv
