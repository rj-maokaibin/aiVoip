from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any


REQUIRED_SERVICES = (
    "backend",
    "reproduction-worker",
    "reproduction-control-high-worker",
    "reproduction-watch-worker",
    "beat",
)

SAFE_ENV_KEYS = (
    "APP_ENV",
    "REPRODUCTION_PLATFORM_MODE",
    "CAPTURE_ENGINE_VERSION",
    "CAPTURE_V2_PRODUCTION_ENABLED",
    "CAPTURE_V2_ACTIVATION_REHEARSAL",
    "CAPTURE_V2_REUSE_LEGACY_REPRODUCTION_SEMANTICS",
    "CAPTURE_V2_RELEASE_GATE_ARTIFACT",
    "CAPTURE_V2_PROFILE_ID",
    "CAPTURE_V2_TRANSPORT",
    "VOIP_PROJECT_NAME",
    "VOIP_DATA_ROOT",
)


def _run(argv: list[str], *, cwd: Path, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=timeout,
    )


def _compose_command(repo_root: Path) -> tuple[list[str] | None, str | None]:
    docker = shutil.which("docker")
    if docker:
        cp = _run([docker, "compose", "version"], cwd=repo_root, timeout=15)
        if cp.returncode == 0:
            return [docker, "compose"], cp.stdout.strip() or cp.stderr.strip()
    legacy = shutil.which("docker-compose")
    if legacy:
        cp = _run([legacy, "version"], cwd=repo_root, timeout=15)
        if cp.returncode == 0:
            return [legacy], cp.stdout.strip() or cp.stderr.strip()
    return None, None


def _safe_env_from_list(values: list[str] | None) -> dict[str, str]:
    result: dict[str, str] = {}
    allowed = set(SAFE_ENV_KEYS)
    for item in values or []:
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        if key in allowed:
            result[key] = value
    return result


def _bool_false(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"", "0", "false", "no", "off"}


def _inspect_container(docker: str, container_id: str, repo_root: Path) -> dict[str, Any]:
    cp = _run([docker, "inspect", container_id], cwd=repo_root, timeout=20)
    if cp.returncode != 0:
        return {"inspect_ok": False, "error": cp.stderr.strip()[:300]}
    try:
        raw = json.loads(cp.stdout)
        row = raw[0]
    except Exception as exc:
        return {"inspect_ok": False, "error": f"INSPECT_JSON_INVALID:{type(exc).__name__}"}
    mounts = [
        {
            "destination": str(m.get("Destination") or ""),
            "read_only": not bool(m.get("RW", True)),
        }
        for m in (row.get("Mounts") or [])
        if str(m.get("Destination") or "") in {
            "/app/validation",
            "/app/profiles",
            "/tmp/voip-reproduction-capture",
            "/tmp/voip-reproduction-objects",
        }
    ]
    state = row.get("State") or {}
    config = row.get("Config") or {}
    return {
        "inspect_ok": True,
        "running": bool(state.get("Running")),
        "status": state.get("Status"),
        "image": str(config.get("Image") or ""),
        "safe_env": _safe_env_from_list(config.get("Env")),
        "mounts": mounts,
    }


def evaluate(repo_root: Path) -> tuple[int, dict[str, Any]]:
    repo_root = repo_root.resolve()
    checks: list[dict[str, Any]] = []
    facts: dict[str, Any] = {
        "scope": "READ_ONLY_DEPLOYMENT_PREFLIGHT",
        "repo_root": str(repo_root),
        "mutations_performed": False,
    }

    compose, version = _compose_command(repo_root)
    checks.append({"name": "compose_available", "passed": compose is not None})
    facts["compose_version"] = version
    if compose is None:
        return 2, {"verdict": "INCONCLUSIVE", "checks": checks, "facts": facts,
                   "reason": "DOCKER_COMPOSE_NOT_AVAILABLE"}

    env_file_raw = str(os.getenv("VOIP_APP_ENV_FILE") or ".env")
    env_file = Path(env_file_raw)
    if not env_file.is_absolute():
        env_file = repo_root / env_file
    facts["env_file"] = str(env_file)
    checks.append({"name": "env_file_present", "passed": env_file.is_file()})

    services_cp = _run([*compose, "config", "--services"], cwd=repo_root, timeout=30)
    if services_cp.returncode != 0:
        facts["compose_config_error"] = services_cp.stderr.strip()[:500]
        checks.append({"name": "compose_config_valid", "passed": False})
        return 2, {"verdict": "INCONCLUSIVE", "checks": checks, "facts": facts,
                   "reason": "COMPOSE_CONFIG_UNAVAILABLE"}
    defined = {line.strip() for line in services_cp.stdout.splitlines() if line.strip()}
    missing = [name for name in REQUIRED_SERVICES if name not in defined]
    checks.append({"name": "required_services_defined", "passed": not missing,
                   "observed": {"missing": missing}})
    facts["defined_required_services"] = [name for name in REQUIRED_SERVICES if name in defined]

    docker = shutil.which("docker")
    if docker is None:
        # Legacy docker-compose still requires the Docker CLI for sanitized inspect.
        checks.append({"name": "docker_cli_available", "passed": False})
        return 2, {"verdict": "INCONCLUSIVE", "checks": checks, "facts": facts,
                   "reason": "DOCKER_CLI_NOT_AVAILABLE"}
    checks.append({"name": "docker_cli_available", "passed": True})

    containers: dict[str, Any] = {}
    for service in REQUIRED_SERVICES:
        if service not in defined:
            continue
        ps = _run([*compose, "ps", "-q", service], cwd=repo_root, timeout=20)
        cid = ps.stdout.strip().splitlines()[0].strip() if ps.returncode == 0 and ps.stdout.strip() else ""
        if not cid:
            containers[service] = {"present": False, "running": False}
            continue
        containers[service] = {"present": True, **_inspect_container(docker, cid, repo_root)}
    facts["containers"] = containers

    missing_running = [
        service for service in REQUIRED_SERVICES
        if not containers.get(service, {}).get("running")
    ]
    checks.append({"name": "required_services_running", "passed": not missing_running,
                   "observed": {"not_running": missing_running}})

    authority_bad: dict[str, dict[str, str]] = {}
    mount_bad: list[str] = []
    for service in REQUIRED_SERVICES:
        item = containers.get(service) or {}
        safe_env = item.get("safe_env") or {}
        if item.get("running"):
            engine = str(safe_env.get("CAPTURE_ENGINE_VERSION") or "V1").upper().strip()
            prod = safe_env.get("CAPTURE_V2_PRODUCTION_ENABLED")
            rehearsal = safe_env.get("CAPTURE_V2_ACTIVATION_REHEARSAL")
            if engine != "V1" or not _bool_false(prod) or not _bool_false(rehearsal):
                authority_bad[service] = {
                    "CAPTURE_ENGINE_VERSION": engine,
                    "CAPTURE_V2_PRODUCTION_ENABLED": str(prod or ""),
                    "CAPTURE_V2_ACTIVATION_REHEARSAL": str(rehearsal or ""),
                }
            destinations = {m.get("destination") for m in item.get("mounts") or []}
            if "/app/validation" not in destinations:
                mount_bad.append(service)
    checks.append({"name": "runtime_v1_authoritative", "passed": not authority_bad,
                   "observed": authority_bad})
    checks.append({"name": "release_artifact_mount_present", "passed": not mount_bad,
                   "observed": {"missing_mount": mount_bad}})

    failed = [row["name"] for row in checks if row.get("passed") is False]
    if failed:
        verdict = "FAIL"
        rc = 1
        reason = "DEPLOYMENT_PREFLIGHT_FAILED"
    else:
        verdict = "PASS"
        rc = 0
        reason = "DEPLOYMENT_PREFLIGHT_READY"
    return rc, {"verdict": verdict, "checks": checks, "facts": facts, "reason": reason}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Capture V2 deployment preflight")
    parser.add_argument("--repo-root", type=Path, required=True)
    args = parser.parse_args(argv)
    rc, payload = evaluate(args.repo_root)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
