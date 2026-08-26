from __future__ import annotations

import argparse
import json
import os
import pwd
import subprocess
from pathlib import Path
from typing import Any, Callable

from app.capture_v2.control import production_cutover_guarded_base as _base


# Older production.env files predate these V2 switches. Missing values have the
# same effective semantics as the application/runtime defaults and therefore do
# not represent a safety downgrade. Explicit values are never overwritten.
_EFFECTIVE_PRESTATE_DEFAULTS = {
    "CAPTURE_ENGINE_VERSION": "V1",
    "CAPTURE_V2_PRODUCTION_ENABLED": "false",
    "CAPTURE_V2_ACTIVATION_REHEARSAL": "false",
}
_DEFAULTED_MARKER = "__CAPTURE_V2_EFFECTIVE_DEFAULTED_KEYS"
_MASTER_SNAPSHOT_REF = "refs/capture-v2/master-snapshot"
_ORIGINAL_READ_SAFE_ENV = _base._read_safe_env
_ORIGINAL_GIT = _base._git


def _read_safe_env_with_effective_defaults(repo_root: Path) -> tuple[int, dict[str, str], str]:
    rc, values, error = _ORIGINAL_READ_SAFE_ENV(repo_root)
    if rc != 0:
        return rc, values, error
    normalized = dict(values)
    defaulted: list[str] = []
    for key, default in _EFFECTIVE_PRESTATE_DEFAULTS.items():
        raw = normalized.get(key)
        if raw is None or str(raw).strip() == "":
            normalized[key] = default
            defaulted.append(key)
    if defaulted:
        normalized[_DEFAULTED_MARKER] = ",".join(sorted(defaulted))
    return rc, normalized, error


def _repo_owner_identity(repo_root: Path) -> tuple[int, str, str]:
    owner_uid = repo_root.stat().st_uid
    account = pwd.getpwuid(owner_uid)
    return owner_uid, account.pw_name, account.pw_dir


def _identity_failure(args: tuple[str, ...], detail: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["/usr/bin/git", *args],
        returncode=126,
        stdout="",
        stderr=f"GIT_REPO_OWNER_IDENTITY_REQUIRED:{detail}",
    )


def _git_failure(args: tuple[str, ...], detail: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["/usr/bin/git", *args],
        returncode=126,
        stdout="",
        stderr=detail,
    )


def _git_as_repo_owner(
    repo_root: Path, *args: str, timeout: float = 120.0
) -> subprocess.CompletedProcess[str]:
    """Run source-validation Git as the repository owner, never as cutover root."""
    try:
        owner_uid, owner_name, owner_home = _repo_owner_identity(repo_root)
    except (KeyError, OSError) as exc:
        return _identity_failure(args, f"OWNER_LOOKUP_FAILED:{type(exc).__name__}")

    effective_uid = os.geteuid()
    if effective_uid == owner_uid:
        return _ORIGINAL_GIT(repo_root, *args, timeout=timeout)

    if effective_uid != 0:
        return _identity_failure(
            args,
            f"EUID_MISMATCH:euid={effective_uid}:owner_uid={owner_uid}",
        )

    argv = [
        "/usr/sbin/runuser",
        "-u",
        owner_name,
        "--",
        "/usr/bin/env",
        f"HOME={owner_home}",
        f"USER={owner_name}",
        f"LOGNAME={owner_name}",
        "/usr/bin/git",
        *args,
    ]
    return _base._run(argv, cwd=repo_root, timeout=timeout)


def _master_snapshot_git() -> tuple[
    Callable[..., subprocess.CompletedProcess[str]], dict[str, str | None]
]:
    """Bind master ancestry checks to an isolated local ref snapshot.

    The production-control checkout is continuously synchronized by a separate
    control loop. Multiple concurrent fetches can therefore leave more than one
    record in the shared ``.git/FETCH_HEAD`` file. Reading ``FETCH_HEAD`` is not
    a safe way to identify the master commit fetched by this cutover.

    Instead, the cutover fetches remote master directly into a dedicated hidden
    local ref and resolves that ref. The subsequent ancestry check is bound to
    the immutable SHA captured from this isolated ref. The control loop does not
    write this ref, so its own fetch activity cannot change which master commit
    is validated during this cutover transaction.
    """
    snapshot: dict[str, str | None] = {
        "master_head": None,
        "master_snapshot_ref": _MASTER_SNAPSHOT_REF,
    }

    def git_for_run(
        repo_root: Path, *args: str, timeout: float = 120.0
    ) -> subprocess.CompletedProcess[str]:
        if args == ("fetch", "origin", "master"):
            isolated_fetch_args = (
                "fetch",
                "--no-tags",
                "origin",
                f"+refs/heads/master:{_MASTER_SNAPSHOT_REF}",
            )
            fetched = _git_as_repo_owner(
                repo_root, *isolated_fetch_args, timeout=timeout
            )
            if fetched.returncode != 0:
                return fetched
            resolved = _git_as_repo_owner(
                repo_root, "rev-parse", _MASTER_SNAPSHOT_REF, timeout=timeout
            )
            if resolved.returncode != 0:
                return _git_failure(
                    args,
                    f"MASTER_SNAPSHOT_REF_RESOLVE_FAILED:{resolved.stderr.strip()}",
                )
            master_head = resolved.stdout.strip().lower()
            if len(master_head) != 40 or any(
                ch not in "0123456789abcdef" for ch in master_head
            ):
                return _git_failure(
                    args, f"MASTER_SNAPSHOT_REF_INVALID:{master_head[:80]}"
                )
            snapshot["master_head"] = master_head
            return fetched

        if (
            len(args) == 4
            and args[0] == "merge-base"
            and args[1] == "--is-ancestor"
            and args[3] == "origin/master"
        ):
            master_head = snapshot.get("master_head")
            if not master_head:
                return _git_failure(args, "MASTER_SNAPSHOT_MISSING")
            return _git_as_repo_owner(
                repo_root,
                args[0],
                args[1],
                args[2],
                master_head,
                timeout=timeout,
            )

        return _git_as_repo_owner(repo_root, *args, timeout=timeout)

    return git_for_run, snapshot


def run(*, repo_root: Path, authorization_path: Path) -> tuple[int, dict[str, Any]]:
    original_read_safe_env = _base._read_safe_env
    original_git = _base._git
    git_for_run, master_snapshot = _master_snapshot_git()
    _base._read_safe_env = _read_safe_env_with_effective_defaults
    _base._git = git_for_run
    try:
        rc, payload = _base.run(repo_root=repo_root, authorization_path=authorization_path)
    finally:
        _base._read_safe_env = original_read_safe_env
        _base._git = original_git

    validated_stage = (payload.get("stages") or {}).get("validated_source_merged")
    if isinstance(validated_stage, dict) and master_snapshot.get("master_head"):
        validated_stage["master_head"] = master_snapshot["master_head"]
        validated_stage["master_ref_source"] = "ISOLATED_LOCAL_REF_SNAPSHOT"
        validated_stage["master_snapshot_ref"] = master_snapshot.get(
            "master_snapshot_ref"
        )

    pre_env = payload.get("pre_env")
    if isinstance(pre_env, dict):
        marker = str(pre_env.pop(_DEFAULTED_MARKER, "") or "")
        if marker:
            payload["pre_env_defaulted_keys"] = [x for x in marker.split(",") if x]
            payload["pre_env_defaults_source"] = "APPLICATION_RUNTIME_DEFAULTS"
    return rc, payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Guarded Capture V2 production cutover with legacy-env default "
            "normalization, repository-owner Git identity, and isolated "
            "master-ref ancestry validation"
        )
    )
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--authorization", type=Path, required=True)
    args = parser.parse_args(argv)
    rc, payload = run(repo_root=args.repo_root, authorization_path=args.authorization)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
