from __future__ import annotations

import subprocess
from pathlib import Path

from app.capture_v2.control import production_cutover_guarded as cutover


def _completed(argv: list[str], rc: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, rc, stdout="ok", stderr="")


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


def test_run_restores_base_hooks_after_failure(monkeypatch, tmp_path: Path) -> None:
    original_read = cutover._base._read_safe_env
    original_git = cutover._base._git

    def explode(*, repo_root: Path, authorization_path: Path):
        assert cutover._base._read_safe_env is cutover._read_safe_env_with_effective_defaults
        assert cutover._base._git is cutover._git_as_repo_owner
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
