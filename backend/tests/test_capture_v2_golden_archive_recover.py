from __future__ import annotations

import io
import tarfile
from datetime import datetime, timedelta, timezone

import pytest

from app.capture_v2.control.policy import ControlPolicy, ControlPolicyError
from app.capture_v2.control.schema import RemoteAction
from app.capture_v2.gate.golden_archive_recover import archive_name_for, inspect_archive


def _action(params):
    now = datetime.now(timezone.utc)
    return RemoteAction.from_dict({
        "schema_version": "capture-v2-remote-action-v1",
        "action_id": "GOLDEN-RECOVER-TEST",
        "sequence": 1,
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
        "action_type": "GOLDEN_ARCHIVE_RECOVER",
        "parameters": params,
        "safety": {"expected_head": "deadbeef"},
    })


def _params(**overrides):
    base = {
        "device_id": "dev-1",
        "model": "APF1250",
        "host": "192.0.2.10",
        "port": 10001,
        "username": "root",
        "platform_id": "mt7621",
        "password_env": "DB:SN-TEST",
        "archive_date": "20260820",
        "timeout_seconds": 120,
    }
    base.update(overrides)
    return base


def test_archive_name_is_constructed_not_user_supplied():
    assert archive_name_for("APF1250", "20260820") == "v21_golden_APF1250_20260820.tar.gz"
    assert archive_name_for("APF3260-M", "20260820") == "v21_golden_APF3260_20260820.tar.gz"
    with pytest.raises(ValueError, match="DATE_INVALID"):
        archive_name_for("APF1250", "../../etc/passwd")
    with pytest.raises(ValueError, match="MODEL_NOT_ALLOWED"):
        archive_name_for("OTHER", "20260820")


def test_policy_builds_only_fixed_recovery_cli(tmp_path):
    (tmp_path / "backend").mkdir()
    command = ControlPolicy(tmp_path).prepare(_action(_params()))
    assert command is not None
    assert command.argv[1:4] == ["-m", "app.capture_v2.gate.golden_archive_recover", "--device-id"]
    assert "--archive-date" in command.argv
    assert "20260820" in command.argv
    assert "/www" not in command.argv
    assert "/tmp" not in command.argv
    assert "archive_name" not in command.argv


def test_policy_rejects_unapproved_model_and_date(tmp_path):
    (tmp_path / "backend").mkdir()
    policy = ControlPolicy(tmp_path)
    with pytest.raises(ControlPolicyError, match="MODEL_NOT_ALLOWED"):
        policy.prepare(_action(_params(model="OTHER")))
    with pytest.raises(ControlPolicyError, match="DATE_INVALID"):
        policy.prepare(_action(_params(archive_date="2026-08-20")))


def test_archive_inventory_is_read_only_and_lists_pcaps(tmp_path):
    path = tmp_path / "golden.tar.gz"
    payload = b"pcap-data"
    with tarfile.open(path, "w:gz") as tf:
        info = tarfile.TarInfo("v21_golden/capture_001.pcap")
        info.size = len(payload)
        tf.addfile(info, io.BytesIO(payload))
        text = b"0 packets dropped by kernel\n"
        info2 = tarfile.TarInfo("v21_golden/tcpdump.stderr")
        info2.size = len(text)
        tf.addfile(info2, io.BytesIO(text))
    inventory = inspect_archive(path)
    assert inventory["member_count"] == 2
    assert inventory["pcap_count"] == 1
    assert inventory["pcap_names"] == ["v21_golden/capture_001.pcap"]
    assert inventory["regular_file_bytes"] == len(payload) + len(text)


def test_archive_inventory_rejects_path_traversal(tmp_path):
    path = tmp_path / "unsafe.tar.gz"
    with tarfile.open(path, "w:gz") as tf:
        info = tarfile.TarInfo("../escape.pcap")
        info.size = 1
        tf.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(ValueError, match="UNSAFE_MEMBER"):
        inspect_archive(path)
