from __future__ import annotations

import subprocess
from pathlib import Path

from app.capture_v2.control import production_cutover_guarded as cutover


def _completed(
    argv: list[str], rc: int = 0, *, stdout: str = "ok", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, rc, stdout=stdout, stderr=stderr)


def test_git_uses_existing_identity_when_already_repo_owner(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cutover, "_repo_owner_identity", lambda _: (1000, "dev", "/home/dev"))
    monkeypatch.setattr(cutover.os, "geteuid", lambda: 1000)
    seen: dict[str, object] = {}

    def fake_original(repo_root: Path, *args: str, timeout: float = 120.0):
        seen["repo_root"] = repo_root
        seen["args"] = args
        seen["timeout"] = timeout
        return _completed(["git", *args])

    monkeypatch.setattr(cutover, "_ORIGINAL_GIT", fake_original)

    cp = cutover._git_as_repo_owner(tmp_path, "fetch", "origin", "master", timeout=77)

    assert cp.returncode == 0
    assert seen == {
        "repo_root": tmp_path,
        "args": ("fetch", "origin", "master"),
        "timeout": 77,
    }


def test_git_drops_root_to_repo_owner_with_owner_home(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cutover, "_repo_owner_identity", lambda _: (1000, "dev", "/home/dev"))
    monkeypatch.setattr(cutover.os, "geteuid", lambda: 0)
    seen: dict[str, object] = {}

    def fake_run(argv: list[str], *, cwd: Path, timeout: float = 600.0):
        seen["argv"] = argv
        seen["cwd"] = cwd
        seen["timeout"] = timeout
        return _completed(argv)

    monkeypatch.setattr(cutover._base, "_run", fake_run)

    cp = cutover._git_as_repo_owner(tmp_path, "fetch", "origin", "master", timeout=88)

    assert cp.returncode == 0
    assert seen["cwd"] == tmp_path
    assert seen["timeout"] == 88
    argv = seen["argv"]
    assert argv == [
        "/usr/sbin/runuser",
        "-u",
        "dev",
        "--",
        "/usr/bin/env",
        "HOME=/home/dev",
        "USER=dev",
        "LOGNAME=dev",
        "/usr/bin/git",
        "fetch",
        "origin",
        "master",
    ]
    assert "/usr/bin/sudo" not in argv


def test_git_fails_closed_for_unprivileged_identity_mismatch(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cutover, "_repo_owner_identity", lambda _: (1000, "dev", "/home/dev"))
    monkeypatch.setattr(cutover.os, "geteuid", lambda: 2000)

    def unexpected_run(*args, **kwargs):  # pragma: no cover - assertion helper
        raise AssertionError("must not execute git under an unproven identity")

    monkeypatch.setattr(cutover._base, "_run", unexpected_run)
    monkeypatch.setattr(cutover, "_ORIGINAL_GIT", unexpected_run)

    cp = cutover._git_as_repo_owner(tmp_path, "fetch", "origin", "master")

    assert cp.returncode == 126
    assert "GIT_REPO_OWNER_IDENTITY_REQUIRED" in cp.stderr
    assert "EUID_MISMATCH:euid=2000:owner_uid=1000" in cp.stderr


def test_master_snapshot_uses_exact_fetch_head_for_ancestor(monkeypatch, tmp_path: Path) -> None:
    master = "e6623e29908aa977332c6e0b87fe04d0a88bce84"
    validated = "db3e8012a9569d9508e9d2cd920baf1de6bac866"
    calls: list[tuple[str, ...]] = []

    def fake_git(repo_root: Path, *args: str, timeout: float = 120.0):
        assert repo_root == tmp_path
        calls.append(args)
        if args == ("fetch", "origin", "master"):
            return _completed(["git", *args])
        if args == ("rev-parse", "FETCH_HEAD"):
            return _completed(["git", *args], stdout=master + "\n")
        if args == ("merge-base", "--is-ancestor", validated, master):
            return _completed(["git", *args])
        raise AssertionError(f"unexpected git args: {args}")

    monkeypatch.setattr(cutover, "_git_as_repo_owner", fake_git)
    git_for_run, snapshot = cutover._master_snapshot_git()

    fetched = git_for_run(tmp_path, "fetch", "origin", "master")
    ancestor = git_for_run(
        tmp_path, "merge-base", "--is-ancestor", validated, "origin/master"
    )

    assert fetched.returncode == 0
    assert ancestor.returncode == 0
    assert snapshot["master_head"] == master
    assert calls == [
        ("fetch", "origin", "master"),
        ("rev-parse", "FETCH_HEAD"),
        ("merge-base", "--is-ancestor", validated, master),
    ]
    assert ("merge-base", "--is-ancestor", validated, "origin/master") not in calls


def test_master_snapshot_fails_closed_if_fetch_head_is_invalid(monkeypatch, tmp_path: Path) -> None:
    def fake_git(repo_root: Path, *args: str, timeout: float = 120.0):
        if args == ("fetch", "origin", "master"):
            return _completed(["git", *args])
        if args == ("rev-parse", "FETCH_HEAD"):
            return _completed(["git", *args], stdout="not-a-sha\n")
        raise AssertionError(f"unexpected git args: {args}")

    monkeypatch.setattr(cutover, "_git_as_repo_owner", fake_git)
    git_for_run, snapshot = cutover._master_snapshot_git()

    cp = git_for_run(tmp_path, "fetch", "origin", "master")

    assert cp.returncode == 126
    assert "MASTER_FETCH_HEAD_INVALID" in cp.stderr
    assert snapshot["master_head"] is None


def test_master_snapshot_fails_closed_if_ancestor_check_has_no_snapshot(
    monkeypatch, tmp_path: Path
) -> None:
    def unexpected_git(*args, **kwargs):  # pragma: no cover - assertion helper
        raise AssertionError("must not execute ancestry check without fetched snapshot")

    monkeypatch.setattr(cutover, "_git_as_repo_owner", unexpected_git)
    git_for_run, snapshot = cutover._master_snapshot_git()

    cp = git_for_run(
        tmp_path,
        "merge-base",
        "--is-ancestor",
        "db3e8012a9569d9508e9d2cd920baf1de6bac866",
        "origin/master",
    )

    assert cp.returncode == 126
    assert cp.stderr == "MASTER_FETCH_SNAPSHOT_MISSING"
    assert snapshot["master_head"] is None


def test_run_restores_base_hooks_after_failure(monkeypatch, tmp_path: Path) -> None:
    original_read = cutover._base._read_safe_env
    original_git = cutover._base._git

    def explode(*, repo_root: Path, authorization_path: Path):
        assert cutover._base._read_safe_env is cutover._read_safe_env_with_effective_defaults
        assert cutover._base._git is not original_git
        assert callable(cutover._base._git)
        raise RuntimeError("boom")

    monkeypatch.setattr(cutover._base, "run", explode)

    try:
        cutover.run(repo_root=tmp_path, authorization_path=tmp_path / "auth.json")
    except RuntimeError as exc:
        assert str(exc) == "boom"
    else:  # pragma: no cover - assertion helper
        raise AssertionError("expected RuntimeError")

    assert cutover._base._read_safe_env is original_read
    assert cutover._base._git is original_git
