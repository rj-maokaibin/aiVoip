import subprocess
from pathlib import Path

import pytest

from app.capture_v2.control.git_sync import GitControlSync, GitSyncError


def _git(repo: Path, *args: str):
    return subprocess.run(["git", *args], cwd=repo, check=True, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _init_repo(repo: Path):
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "capture-v2-test@example.invalid")
    _git(repo, "config", "user.name", "Capture V2 Test")


def test_commit_sync_rejects_path_outside_repo(tmp_path):
    _init_repo(tmp_path)
    sync = GitControlSync(tmp_path, branch="main")
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("x")
    with pytest.raises(GitSyncError, match="PATH_OUTSIDE_REPO"):
        sync.commit_and_push([outside], message="no")


def test_git_command_timeout_is_structured(tmp_path, monkeypatch):
    _init_repo(tmp_path)
    sync = GitControlSync(tmp_path, branch="main", command_timeout_seconds=0.01)

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs.get("timeout"))

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(GitSyncError, match="GIT_COMMAND_TIMEOUT:fetch"):
        sync._run("fetch", "origin", "main")


def test_control_divergence_refuses_product_commit(tmp_path):
    _init_repo(tmp_path)
    control = tmp_path / "validation/control/status.json"
    control.parent.mkdir(parents=True)
    control.write_text("{}\n")
    product = tmp_path / "backend/product.py"
    product.parent.mkdir(parents=True)
    product.write_text("BASE = 1\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "base")
    base = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

    product.write_text("BASE = 2\n")
    _git(tmp_path, "add", "backend/product.py")
    _git(tmp_path, "commit", "-qm", "product local")

    sync = GitControlSync(tmp_path, branch="main")
    with pytest.raises(GitSyncError, match="CONTROL_DIVERGENCE_TOUCHED_PRODUCT_CODE"):
        sync._control_only_local_commits(base)


def test_control_only_local_commit_is_accepted(tmp_path):
    _init_repo(tmp_path)
    control = tmp_path / "validation/control/status.json"
    control.parent.mkdir(parents=True)
    control.write_text("{}\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "base")
    base = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

    control.write_text('{"state":"PASS"}\n')
    _git(tmp_path, "add", "validation/control/status.json")
    _git(tmp_path, "commit", "-qm", "control result")

    sync = GitControlSync(tmp_path, branch="main")
    commits = sync._control_only_local_commits(base)
    assert len(commits) == 1
