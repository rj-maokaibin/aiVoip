from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from app.capture_v2.control.activation_rehearsal import _compose, _down, run


def _project_empty(repo_root: Path, env_file: Path) -> bool:
    cp = _compose(repo_root, env_file, ["ps", "-q"], timeout=60, check=False)
    return cp.returncode == 0 and not cp.stdout.strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Guarded bounded Capture V2 activation rehearsal"
    )
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--action-id", required=True)
    parser.add_argument("--sn", required=True)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument("--platform-id", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--profile-id", default="VOIP_GENERIC_FULL_CAPTURE")
    parser.add_argument("--observation-seconds", type=float, default=20.0)
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    original_env = Path(os.getenv("VOIP_APP_ENV_FILE") or (repo_root / ".env"))
    if not original_env.is_absolute():
        original_env = repo_root / original_env

    # Keep the isolated rehearsal away from any host ports already used by the
    # validation server. Internal service ports stay unchanged.
    os.environ["VOIP_BACKEND_BIND"] = "127.0.0.1"
    os.environ["VOIP_BACKEND_PORT"] = "18000"
    os.environ["VOIP_MINIO_CONSOLE_BIND"] = "127.0.0.1"
    os.environ["VOIP_MINIO_CONSOLE_PORT"] = "19001"

    preclean_error = None
    try:
        _down(repo_root, original_env)
        if not _project_empty(repo_root, original_env):
            preclean_error = "REHEARSAL_PROJECT_NOT_EMPTY_AFTER_PRECLEAN"
    except Exception as exc:
        preclean_error = f"REHEARSAL_PRECLEAN_FAILED:{type(exc).__name__}:{exc}"

    if preclean_error:
        payload = {
            "verdict": "INCONCLUSIVE",
            "reason": "REHEARSAL_PRECLEAN_NOT_PROVEN",
            "error": preclean_error,
            "production_v2_enabled": False,
            "final_authority_target": "V1",
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 2

    rc = 2
    payload: dict = {}
    final_error = None
    try:
        rc, payload = run(args)
    finally:
        try:
            _down(repo_root, original_env)
            if not _project_empty(repo_root, original_env):
                final_error = "REHEARSAL_PROJECT_RESIDUAL_CONTAINERS"
        except Exception as exc:
            final_error = f"REHEARSAL_FINAL_DOWN_FAILED:{type(exc).__name__}:{exc}"

    payload["guarded_rehearsal"] = True
    payload["host_ports"] = {
        "backend": "127.0.0.1:18000",
        "minio_console": "127.0.0.1:19001",
    }
    payload["final_rehearsal_project_empty"] = final_error is None
    if final_error:
        payload["guard_cleanup_error"] = final_error
        payload["verdict"] = "FAIL"
        payload["reason"] = "REHEARSAL_FINAL_SERVER_RESTORE_NOT_PROVEN"
        payload["production_v2_enabled"] = False
        payload["final_authority_target"] = "V1"
        rc = 1

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
