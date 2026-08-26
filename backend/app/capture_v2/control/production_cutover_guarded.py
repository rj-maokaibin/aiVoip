from __future__ import annotations

import argparse
import json
import os
import pwd
import socket
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
_MINIO_CONSOLE_BIND = "127.0.0.1"
_MINIO_CONSOLE_PORT = 19001
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


def _cutover_env_overrides() -> dict[str, str]:
    """Return the complete, audited production env delta for Capture V2 cutover."""
    return {
        "REPRODUCTION_PLATFORM_MODE": "real",
        "CAPTURE_ENGINE_VERSION": "V2",
        "CAPTURE_V2_PRODUCTION_ENABLED": "true",
        "CAPTURE_V2_ACTIVATION_REHEARSAL": "false",
        "CAPTURE_V2_REUSE_LEGACY_REPRODUCTION_SEMANTICS": "false",
        "CAPTURE_V2_RELEASE_GATE_ARTIFACT": "/app/validation/capture_v2_release_gate.json",
        # The existing live `aivoip` stack legitimately owns host 9001. Keep
        # the new `voip-ai` MinIO console local-only and on its own host port.
        # Container-internal MinIO ports remain unchanged (9000 API / 9001 UI).
        "VOIP_MINIO_CONSOLE_BIND": _MINIO_CONSOLE_BIND,
        "VOIP_MINIO_CONSOLE_PORT": str(_MINIO_CONSOLE_PORT),
    }


def _minio_console_port_available() -> tuple[bool, str]:
    """Check the exact host bind before any production.env mutation."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((_MINIO_CONSOLE_BIND, _MINIO_CONSOLE_PORT))
        return True, ""
    except OSError as exc:
        return False, f"{type(exc).__name__}:{exc}"
    finally:
        probe.close()


def _write_cutover_env_with_minio_console_isolation(
    repo_root: Path, backup_path: Path
) -> tuple[int, dict[str, Any], str]:
    available, port_error = _minio_console_port_available()
    if not available:
        return (
            98,
            {},
            f"MINIO_CONSOLE_HOST_PORT_UNAVAILABLE:{_MINIO_CONSOLE_BIND}:{_MINIO_CONSOLE_PORT}:{port_error}",
        )

    overrides = _cutover_env_overrides()
    code = r'''
import json, os, shutil, stat
from pathlib import Path
src=Path("/etc/voip-ai/production.env")
backup=Path(os.environ["BACKUP_PATH"])
overrides=json.loads(os.environ["CUTOVER_OVERRIDES_JSON"])
if not isinstance(overrides, dict) or not overrides:
    raise SystemExit("CUTOVER_OVERRIDES_INVALID")
if any(not isinstance(k,str) or not isinstance(v,str) for k,v in overrides.items()):
    raise SystemExit("CUTOVER_OVERRIDES_INVALID")
mode=stat.S_IMODE(src.stat().st_mode)
if mode & 0o077:
    raise SystemExit("PRODUCTION_ENV_PERMISSIONS_NOT_PRIVATE")
shutil.copy2(src,backup); os.chmod(backup,0o600)
lines=src.read_text(encoding="utf-8").splitlines(); out=[]; seen=set()
for raw in lines:
    stripped=raw.strip()
    if stripped and not stripped.startswith("#") and "=" in stripped:
        probe=stripped[7:].strip() if stripped.startswith("export ") else stripped
        key=probe.split("=",1)[0].strip()
        if key in overrides:
            prefix="export " if stripped.startswith("export ") else ""
            out.append(f"{prefix}{key}={overrides[key]}"); seen.add(key); continue
    out.append(raw)
for key,value in overrides.items():
    if key not in seen: out.append(f"{key}={value}")
tmp=src.with_name(src.name+".capture-v2-new")
tmp.write_text("\n".join(out)+"\n",encoding="utf-8"); os.chmod(tmp,0o600); os.replace(tmp,src)
print(json.dumps({"backup":str(backup),"overrides":overrides},sort_keys=True))
'''
    cp = _base._sudo(
        "/usr/bin/env",
        f"BACKUP_PATH={backup_path}",
        "CUTOVER_OVERRIDES_JSON=" + json.dumps(overrides, sort_keys=True, separators=(",", ":")),
        "/usr/bin/python3",
        "-c",
        code,
        cwd=repo_root,
        timeout=60,
    )
    if cp.returncode != 0:
        return cp.returncode, {}, _base._safe_tail(cp.stderr or cp.stdout)
    try:
        return 0, json.loads(cp.stdout.strip()), ""
    except Exception as exc:
        return 3, {}, f"CUTOVER_ENV_JSON_INVALID:{type(exc).__name__}"


def run(*, repo_root: Path, authorization_path: Path) -> tuple[int, dict[str, Any]]:
    original_read_safe_env = _base._read_safe_env
    original_git = _base._git
    original_write_cutover_env = _base._write_cutover_env
    git_for_run, master_snapshot = _master_snapshot_git()
    _base._read_safe_env = _read_safe_env_with_effective_defaults
    _base._git = git_for_run
    _base._write_cutover_env = _write_cutover_env_with_minio_console_isolation
    try:
        rc, payload = _base.run(repo_root=repo_root, authorization_path=authorization_path)
    finally:
        _base._read_safe_env = original_read_safe_env
        _base._git = original_git
        _base._write_cutover_env = original_write_cutover_env

    validated_stage = (payload.get("stages") or {}).get("validated_source_merged")
    if isinstance(validated_stage, dict) and master_snapshot.get("master_head"):
        validated_stage["master_head"] = master_snapshot["master_head"]
        validated_stage["master_ref_source"] = "ISOLATED_LOCAL_REF_SNAPSHOT"
        validated_stage["master_snapshot_ref"] = master_snapshot.get(
            "master_snapshot_ref"
        )

    payload["minio_console_host_isolation"] = {
        "bind": _MINIO_CONSOLE_BIND,
        "port": _MINIO_CONSOLE_PORT,
        "container_console_port": 9001,
    }

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
            "normalization, repository-owner Git identity, isolated master-ref "
            "ancestry validation, and collision-free local MinIO console binding"
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
