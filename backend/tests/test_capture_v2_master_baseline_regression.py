import subprocess
from pathlib import Path

from app.capture_v2 import gate_cli
from app.capture_v2.control import master_baseline_regression, master_fix_candidate_regression


def test_gate_cli_bounded_master_dispatch_only_for_exact_sha(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    module = repo / "backend/app/capture_v2/gate_cli.py"
    module.parent.mkdir(parents=True)
    module.write_text("# marker\n")
    calls = []

    def fake_main(argv):
        calls.append(argv)
        return 0

    monkeypatch.setattr(gate_cli, "__file__", str(module))
    monkeypatch.setattr(master_baseline_regression, "main", fake_main)
    sha = "a" * 40
    rc = gate_cli._bounded_master_baseline_regression([
        "evaluate", "--bundle", sha,
        "--gate-id", "MASTER-BASELINE-INTEGRATION-RC59",
    ])
    assert rc == 0
    assert calls == [["--repo-root", str(repo), "--master-sha", sha]]

    calls.clear()
    assert gate_cli._bounded_master_baseline_regression([
        "evaluate", "--bundle", "master",
        "--gate-id", "MASTER-BASELINE-INTEGRATION-RC59",
    ]) is None
    assert gate_cli._bounded_master_baseline_regression([
        "evaluate", "--bundle", sha, "--gate-id", "R3-01",
    ]) is None
    assert calls == []


def test_gate_cli_master_fix_candidate_dispatch_is_exact_and_fail_closed(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    module = repo / "backend/app/capture_v2/gate_cli.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text("# marker\n")
    calls = []

    def fake_main(argv):
        calls.append(argv)
        return 0

    monkeypatch.setattr(gate_cli, "__file__", str(module))
    monkeypatch.setattr(master_fix_candidate_regression, "main", fake_main)
    sha = "c962c0d174099bae1afc8db55067402b36717487"
    rc = gate_cli._bounded_master_fix_candidate_regression([
        "evaluate", "--bundle", sha,
        "--gate-id", "MASTER-FIX-CANDIDATE-INTEGRATION-RC60",
    ])
    assert rc == 0
    assert calls == [["--repo-root", str(repo), "--candidate-sha", sha]]

    calls.clear()
    assert gate_cli._bounded_master_fix_candidate_regression([
        "evaluate", "--bundle", "a" * 40,
        "--gate-id", "MASTER-FIX-CANDIDATE-INTEGRATION-RC60",
    ]) is None
    assert gate_cli._bounded_master_fix_candidate_regression([
        "evaluate", "--bundle", sha, "--gate-id", "R3-01",
    ]) is None
    assert calls == []


def test_master_regression_rejects_invalid_sha_without_git(tmp_path):
    rc, payload = master_baseline_regression.run(repo_root=tmp_path, master_sha="master")
    assert rc == 1
    assert payload["verdict"] == "FAIL"
    assert payload["reason"] == "MASTER_SHA_INVALID"
    assert payload["branch_mutation"] is False
    assert payload["merge_commit_created"] is False


def test_master_fix_candidate_rejects_unapproved_sha_without_git(tmp_path):
    rc, payload = master_fix_candidate_regression.run(
        repo_root=tmp_path, candidate_sha="a" * 40
    )
    assert rc == 1
    assert payload["verdict"] == "FAIL"
    assert payload["reason"] == "MASTER_FIX_CANDIDATE_SHA_NOT_AUDITED"
    assert payload["branch_mutation"] is False
    assert payload["merge_commit_created"] is False


def test_master_docker_env_falls_back_to_noninteractive_sudo(monkeypatch, tmp_path):
    def fake_which(name, path=None):
        return {"docker": "/usr/bin/docker", "sudo": "/usr/bin/sudo"}.get(name)

    def fake_run(argv, **kwargs):
        argv = list(argv)
        if argv == ["/usr/bin/docker", "info"]:
            return subprocess.CompletedProcess(argv, 1, "", "permission denied")
        return subprocess.CompletedProcess(argv, 0, "ok\n", "")

    monkeypatch.setattr(master_baseline_regression.shutil, "which", fake_which)
    monkeypatch.setattr(master_baseline_regression, "_run", fake_run)
    env, authority = master_baseline_regression._docker_env(tmp_path, tmp_path)
    assert authority == "SUDO_NONINTERACTIVE_SHIM"
    shim = tmp_path / "docker-shim/docker"
    assert shim.is_file()
    assert env["PATH"].split(":", 1)[0] == str(shim.parent)
