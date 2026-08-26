from __future__ import annotations

import json
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


def test_master_snapshot_uses_isolated_ref_for_ancestor(monkeypatch, tmp_path: Path) -> None:
    master = "abcb054d018b27495aaa4c47079c354b69471a9d"
    validated = "db3e8012a9569d9508e9d2cd920baf1de6bac866"
    snapshot_ref = "refs/capture-v2/master-snapshot"
    calls: list[tuple[str, ...]] = []

    def fake_git(repo_root: Path, *args: str, timeout: float = 120.0):
        assert repo_root == tmp_path
        calls.append(args)
        if args == (
            "fetch",
            "--no-tags",
            "origin",
            f"+refs/heads/master:{snapshot_ref}",
        ):
            return _completed(["git", *args])
        if args == ("rev-parse", snapshot_ref):
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
    assert snapshot["master_snapshot_ref"] == snapshot_ref
    assert calls == [
        (
            "fetch",
            "--no-tags",
            "origin",
            f"+refs/heads/master:{snapshot_ref}",
        ),
        ("rev-parse", snapshot_ref),
        ("merge-base", "--is-ancestor", validated, master),
    ]
    assert not any("FETCH_HEAD" in arg for call in calls for arg in call)
    assert ("merge-base", "--is-ancestor", validated, "origin/master") not in calls


def test_master_snapshot_ignores_shared_fetch_head(monkeypatch, tmp_path: Path) -> None:
    master = "abcb054d018b27495aaa4c47079c354b69471a9d"
    control = "9df273ab29569afb198f5e0bcf36fe7750f07192"
    snapshot_ref = "refs/capture-v2/master-snapshot"
    calls: list[tuple[str, ...]] = []

    def fake_git(repo_root: Path, *args: str, timeout: float = 120.0):
        calls.append(args)
        if args == (
            "fetch",
            "--no-tags",
            "origin",
            f"+refs/heads/master:{snapshot_ref}",
        ):
            return _completed(["git", *args])
        if args == ("rev-parse", snapshot_ref):
            return _completed(["git", *args], stdout=master + "\n")
        if args == ("rev-parse", "FETCH_HEAD"):
            raise AssertionError(
                f"shared FETCH_HEAD must never be read; it could point at {control}"
            )
        raise AssertionError(f"unexpected git args: {args}")

    monkeypatch.setattr(cutover, "_git_as_repo_owner", fake_git)
    git_for_run, snapshot = cutover._master_snapshot_git()

    cp = git_for_run(tmp_path, "fetch", "origin", "master")

    assert cp.returncode == 0
    assert snapshot["master_head"] == master
    assert ("rev-parse", "FETCH_HEAD") not in calls


def test_master_snapshot_fails_closed_if_isolated_ref_is_invalid(
    monkeypatch, tmp_path: Path
) -> None:
    snapshot_ref = "refs/capture-v2/master-snapshot"

    def fake_git(repo_root: Path, *args: str, timeout: float = 120.0):
        if args == (
            "fetch",
            "--no-tags",
            "origin",
            f"+refs/heads/master:{snapshot_ref}",
        ):
            return _completed(["git", *args])
        if args == ("rev-parse", snapshot_ref):
            return _completed(["git", *args], stdout="not-a-sha\n")
        raise AssertionError(f"unexpected git args: {args}")

    monkeypatch.setattr(cutover, "_git_as_repo_owner", fake_git)
    git_for_run, snapshot = cutover._master_snapshot_git()

    cp = git_for_run(tmp_path, "fetch", "origin", "master")

    assert cp.returncode == 126
    assert "MASTER_SNAPSHOT_REF_INVALID" in cp.stderr
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
    assert cp.stderr == "MASTER_SNAPSHOT_MISSING"
    assert snapshot["master_head"] is None


def test_cutover_env_overrides_isolate_minio_console() -> None:
    overrides = cutover._cutover_env_overrides()

    assert overrides["REPRODUCTION_PLATFORM_MODE"] == "real"
    assert overrides["CAPTURE_ENGINE_VERSION"] == "V2"
    assert overrides["CAPTURE_V2_PRODUCTION_ENABLED"] == "true"
    assert overrides["VOIP_MINIO_CONSOLE_BIND"] == "127.0.0.1"
    assert overrides["VOIP_MINIO_CONSOLE_PORT"] == "19001"


def test_write_cutover_env_fails_before_sudo_when_minio_port_busy(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        cutover,
        "_minio_console_port_available",
        lambda: (False, "OSError:[Errno 98] Address already in use"),
    )

    def unexpected_sudo(*args, **kwargs):  # pragma: no cover - assertion helper
        raise AssertionError("production.env must not be touched when host port is busy")

    monkeypatch.setattr(cutover._base, "_sudo", unexpected_sudo)

    rc, change, error = cutover._write_cutover_env_with_minio_console_isolation(
        tmp_path, Path("/etc/voip-ai/backup-test")
    )

    assert rc == 98
    assert change == {}
    assert "MINIO_CONSOLE_HOST_PORT_UNAVAILABLE:127.0.0.1:19001" in error


def test_write_cutover_env_passes_fixed_minio_overrides_to_root_helper(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cutover, "_minio_console_port_available", lambda: (True, ""))
    seen: dict[str, object] = {}

    def fake_sudo(*argv: str, cwd: Path, timeout: float = 600.0):
        seen["argv"] = argv
        seen["cwd"] = cwd
        seen["timeout"] = timeout
        env_arg = next(x for x in argv if x.startswith("CUTOVER_OVERRIDES_JSON="))
        overrides = json.loads(env_arg.split("=", 1)[1])
        seen["overrides"] = overrides
        payload = {"backup": "/etc/voip-ai/backup-test", "overrides": overrides}
        return _completed(list(argv), stdout=json.dumps(payload))

    monkeypatch.setattr(cutover._base, "_sudo", fake_sudo)

    rc, change, error = cutover._write_cutover_env_with_minio_console_isolation(
        tmp_path, Path("/etc/voip-ai/backup-test")
    )

    assert rc == 0
    assert error == ""
    assert seen["cwd"] == tmp_path
    assert seen["timeout"] == 60
    overrides = seen["overrides"]
    assert overrides["VOIP_MINIO_CONSOLE_BIND"] == "127.0.0.1"
    assert overrides["VOIP_MINIO_CONSOLE_PORT"] == "19001"
    assert change["overrides"] == overrides


def test_run_restores_base_hooks_after_failure(monkeypatch, tmp_path: Path) -> None:
    original_read = cutover._base._read_safe_env
    original_git = cutover._base._git
    original_write = cutover._base._write_cutover_env

    def explode(*, repo_root: Path, authorization_path: Path):
        assert cutover._base._read_safe_env is cutover._read_safe_env_with_effective_defaults
        assert cutover._base._git is not original_git
        assert callable(cutover._base._git)
        assert (
            cutover._base._write_cutover_env
            is cutover._write_cutover_env_with_minio_console_isolation
        )
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
    assert cutover._base._write_cutover_env is original_write
