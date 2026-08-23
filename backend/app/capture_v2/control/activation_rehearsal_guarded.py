from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from app.capture_v2.control import activation_rehearsal as rehearsal
from app.capture_v2.control.activation_rehearsal import _compose, _down, _safe_error, run


DIAG_SERVICES = (
    "backend",
    "postgres",
    "redis",
    "minio",
    "reproduction-worker",
    "reproduction-control-high-worker",
    "reproduction-watch-worker",
    "beat",
)


def _project_empty(repo_root: Path, env_file: Path) -> bool:
    cp = _compose(repo_root, env_file, ["ps", "-q"], timeout=60, check=False)
    return cp.returncode == 0 and not cp.stdout.strip()


def _collect_diagnostics(repo_root: Path, env_file: Path) -> dict:
    out: dict = {}
    ps = _compose(repo_root, env_file, ["ps", "-a"], timeout=60, check=False)
    out["compose_ps"] = _safe_error((ps.stdout or "") + "\n" + (ps.stderr or ""))
    logs: dict[str, str] = {}
    for service in DIAG_SERVICES:
        cp = _compose(
            repo_root,
            env_file,
            ["logs", "--no-color", "--tail", "80", service],
            timeout=60,
            check=False,
        )
        text = (cp.stdout or "") + "\n" + (cp.stderr or "")
        logs[service] = _safe_error(text)
    out["logs_tail"] = logs
    return out


def _install_backend_health_wait(timeout: float = 120.0) -> None:
    single_probe = rehearsal._backend_http_health

    def _wait(repo_root: Path, env_file: Path) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if single_probe(repo_root, env_file):
                return True
            time.sleep(2)
        return False

    rehearsal._backend_http_health = _wait


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

    base_compose = repo_root / "docker-compose.yml"
    rehearsal_compose = repo_root / "docker-compose.capture-v2-rehearsal.yml"
    if not rehearsal_compose.is_file():
        payload = {
            "verdict": "INCONCLUSIVE",
            "reason": "REHEARSAL_COMPOSE_OVERRIDE_MISSING",
            "production_v2_enabled": False,
            "final_authority_target": "V1",
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 2
    os.environ["COMPOSE_FILE"] = os.pathsep.join(
        [str(base_compose), str(rehearsal_compose)]
    )
    _install_backend_health_wait()

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
        if rc != 0:
            try:
                payload["diagnostics"] = _collect_diagnostics(repo_root, original_env)
            except Exception as exc:
                payload["diagnostics_error"] = f"{type(exc).__name__}:{_safe_error(str(exc))}"
    finally:
        try:
            _down(repo_root, original_env)
            if not _project_empty(repo_root, original_env):
                final_error = "REHEARSAL_PROJECT_RESIDUAL_CONTAINERS"
        except Exception as exc:
            final_error = f"REHEARSAL_FINAL_DOWN_FAILED:{type(exc).__name__}:{exc}"

    payload["guarded_rehearsal"] = True
    payload["host_port_publishing"] = "DISABLED_BY_COMPOSE_OVERRIDE"
    payload["backend_health_wait_seconds"] = 120
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
