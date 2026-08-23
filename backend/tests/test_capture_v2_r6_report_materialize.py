from pathlib import Path
import subprocess

import pytest

from app.capture_v2 import gate_cli
from app.capture_v2.control import r6_report_materialize, r6_report_materialize_guarded


def test_gate_cli_bounded_r6_dispatch_only_for_exact_golden(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    module = repo / "backend/app/capture_v2/gate_cli.py"
    golden = repo / "validation/capture_v2/R6_APF1250_FIRST_8000_ABNORMAL_GOLDEN_RC33.json"
    module.parent.mkdir(parents=True)
    golden.parent.mkdir(parents=True)
    module.write_text("# marker\n")
    golden.write_text("{}\n")

    calls = []

    def fake_materialize(argv):
        calls.append(argv)
        return 0

    monkeypatch.setattr(gate_cli, "__file__", str(module))
    monkeypatch.setattr(r6_report_materialize_guarded, "main", fake_materialize)

    rc = gate_cli._bounded_r6_materialization([
        "evaluate",
        "--bundle", str(golden),
        "--gate-id", "R6-PRODUCT-REPORT-MATERIALIZE-RC56",
    ])
    assert rc == 0
    assert calls == [[
        "--repo-root", str(repo),
        "--golden-path", str(golden.resolve()),
    ]]

    calls.clear()
    assert gate_cli._bounded_r6_materialization([
        "evaluate", "--bundle", str(golden), "--gate-id", "R3-01"
    ]) is None
    assert calls == []

    other = repo / "validation/capture_v2/other.json"
    other.write_text("{}\n")
    assert gate_cli._bounded_r6_materialization([
        "evaluate", "--bundle", str(other),
        "--gate-id", "R6-PRODUCT-REPORT-MATERIALIZE-RC56",
    ]) is None
    assert calls == []


def test_guarded_docker_tools_prefers_direct_daemon(monkeypatch, tmp_path):
    calls = []

    def fake_which(name):
        return {"docker": "/usr/bin/docker", "sudo": "/usr/bin/sudo"}.get(name)

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "ok\n", "")

    monkeypatch.setattr(r6_report_materialize_guarded.shutil, "which", fake_which)
    monkeypatch.setattr(r6_report_materialize_guarded, "_run", fake_run)

    docker_cmd, compose, authority = r6_report_materialize_guarded._docker_tools(tmp_path)
    assert docker_cmd == ["/usr/bin/docker"]
    assert compose == ["/usr/bin/docker", "compose"]
    assert authority == "DIRECT"
    assert ["/usr/bin/sudo", "-n", "/usr/bin/docker", "ps", "-q"] not in calls


def test_guarded_docker_tools_falls_back_to_noninteractive_sudo(monkeypatch, tmp_path):
    calls = []

    def fake_which(name):
        return {"docker": "/usr/bin/docker", "sudo": "/usr/bin/sudo"}.get(name)

    def fake_run(argv, **kwargs):
        argv = list(argv)
        calls.append(argv)
        if argv == ["/usr/bin/docker", "ps", "-q"]:
            return subprocess.CompletedProcess(argv, 1, "", "permission denied")
        return subprocess.CompletedProcess(argv, 0, "ok\n", "")

    monkeypatch.setattr(r6_report_materialize_guarded.shutil, "which", fake_which)
    monkeypatch.setattr(r6_report_materialize_guarded, "_run", fake_run)

    docker_cmd, compose, authority = r6_report_materialize_guarded._docker_tools(tmp_path)
    assert docker_cmd == ["/usr/bin/sudo", "-n", "/usr/bin/docker"]
    assert compose == ["/usr/bin/sudo", "-n", "/usr/bin/docker", "compose"]
    assert authority == "SUDO_NONINTERACTIVE"
    assert ["/usr/bin/sudo", "-n", "/usr/bin/docker", "ps", "-q"] in calls


def test_guarded_docker_tools_fails_closed_when_sudo_is_not_authorized(monkeypatch, tmp_path):
    def fake_which(name):
        return {"docker": "/usr/bin/docker", "sudo": "/usr/bin/sudo"}.get(name)

    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, "", "not allowed")

    monkeypatch.setattr(r6_report_materialize_guarded.shutil, "which", fake_which)
    monkeypatch.setattr(r6_report_materialize_guarded, "_run", fake_run)

    with pytest.raises(RuntimeError, match="DOCKER_DAEMON_PERMISSION_DENIED"):
        r6_report_materialize_guarded._docker_tools(tmp_path)


def test_golden_loader_rejects_wrong_finding(tmp_path):
    path = tmp_path / "golden.json"
    path.write_text(
        '{"schema_version":"capture-v2-r6-abnormal-golden-v1",'
        '"expected_panel_target":"8000","observed_dtmf_digits":"000",'
        '"finding":{"conclusion":"OVERCLAIMED_ROOT_CAUSE"}}\n'
    )
    with pytest.raises(RuntimeError, match="R6_GOLDEN_FINDING_MISMATCH"):
        r6_report_materialize._load_golden(path)


def test_overlap_handles_single_timestamp_point():
    from datetime import datetime, timezone

    start = datetime(2026, 8, 23, 7, 5, 21, tzinfo=timezone.utc)
    end = datetime(2026, 8, 23, 7, 5, 59, tzinfo=timezone.utc)
    point = datetime(2026, 8, 23, 7, 5, 32, tzinfo=timezone.utc)
    assert r6_report_materialize._overlaps(point, point, start, end) is True
