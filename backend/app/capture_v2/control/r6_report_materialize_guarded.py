from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from sqlalchemy.engine import make_url


PROJECT = "capture-v2-r6-materialize"


def _safe_error(value: str) -> str:
    lines = []
    for raw in str(value or "").splitlines()[-40:]:
        upper = raw.upper()
        if any(token in upper for token in ("PASSWORD", "SECRET", "TOKEN=", "DATABASE_URL")):
            lines.append("[REDACTED_SENSITIVE_LINE]")
        else:
            lines.append(raw[:500])
    return "\n".join(lines)[-6000:]


def _run(argv: list[str], *, cwd: Path, env: dict[str, str] | None = None,
         timeout: float = 120.0, check: bool = True) -> subprocess.CompletedProcess[str]:
    cp = subprocess.run(
        argv,
        cwd=cwd,
        env=env or os.environ.copy(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
        shell=False,
    )
    if check and cp.returncode != 0:
        raise RuntimeError(
            f"COMMAND_FAILED:{argv[0]}:rc={cp.returncode}:"
            f"{_safe_error(cp.stderr or cp.stdout)}"
        )
    return cp


def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _docker_tools(repo_root: Path) -> tuple[str, list[str]]:
    docker = shutil.which("docker")
    if not docker:
        raise RuntimeError("DOCKER_NOT_AVAILABLE")
    cp = _run([docker, "compose", "version"], cwd=repo_root, timeout=20, check=False)
    if cp.returncode != 0:
        raise RuntimeError("DOCKER_COMPOSE_NOT_AVAILABLE")
    return docker, [docker, "compose"]


def _running_postgres_containers(docker: str, repo_root: Path) -> list[str]:
    cp = _run(
        [docker, "ps", "--filter", "label=com.docker.compose.service=postgres", "-q"],
        cwd=repo_root, timeout=20, check=False,
    )
    if cp.returncode != 0:
        return []
    return [line.strip() for line in cp.stdout.splitlines() if line.strip()]


def _container_ip(docker: str, repo_root: Path, cid: str) -> str:
    cp = _run(
        [docker, "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}", cid],
        cwd=repo_root, timeout=20,
    )
    values = [item for item in cp.stdout.strip().split() if item]
    if len(values) != 1:
        raise RuntimeError(f"POSTGRES_CONTAINER_IP_AMBIGUOUS:{len(values)}")
    return values[0]


def _wait_postgres_healthy(docker: str, repo_root: Path, cid: str, timeout: float = 120.0) -> None:
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        cp = _run(
            [docker, "inspect", "-f", "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}", cid],
            cwd=repo_root, timeout=20, check=False,
        )
        last = cp.stdout.strip().lower()
        if cp.returncode == 0 and last in {"healthy", "running"}:
            return
        time.sleep(2)
    raise RuntimeError(f"POSTGRES_NOT_HEALTHY:{last}")


def _start_temp_postgres(repo_root: Path, env_file: Path, compose: list[str], docker: str) -> str:
    env = os.environ.copy()
    env["COMPOSE_PROJECT_NAME"] = PROJECT
    _run(
        [*compose, "--env-file", str(env_file), "-p", PROJECT, "down", "--remove-orphans"],
        cwd=repo_root, env=env, timeout=120, check=False,
    )
    _run(
        [*compose, "--env-file", str(env_file), "-p", PROJECT, "up", "-d", "postgres"],
        cwd=repo_root, env=env, timeout=300,
    )
    cp = _run(
        [*compose, "--env-file", str(env_file), "-p", PROJECT, "ps", "-q", "postgres"],
        cwd=repo_root, env=env, timeout=30,
    )
    cid = cp.stdout.strip().splitlines()[0].strip() if cp.stdout.strip() else ""
    if not cid:
        raise RuntimeError("TEMP_POSTGRES_CONTAINER_NOT_FOUND")
    _wait_postgres_healthy(docker, repo_root, cid)
    return cid


def _stop_temp_postgres(repo_root: Path, env_file: Path, compose: list[str]) -> None:
    env = os.environ.copy()
    env["COMPOSE_PROJECT_NAME"] = PROJECT
    _run(
        [*compose, "--env-file", str(env_file), "-p", PROJECT, "down", "--remove-orphans"],
        cwd=repo_root, env=env, timeout=180, check=False,
    )


def _database_url_for_ip(env_values: dict[str, str], ip: str) -> str:
    raw = str(env_values.get("DATABASE_URL") or "").strip()
    if not raw:
        raise RuntimeError("DATABASE_URL_MISSING")
    try:
        url = make_url(raw)
    except Exception as exc:
        raise RuntimeError(f"DATABASE_URL_INVALID:{type(exc).__name__}") from exc
    if not url.database:
        raise RuntimeError("DATABASE_URL_DATABASE_MISSING")
    return url.set(host=ip, port=url.port or 5432).render_as_string(hide_password=False)


def run(*, repo_root: Path, golden_path: Path) -> tuple[int, dict[str, Any]]:
    repo_root = repo_root.resolve()
    golden_path = golden_path.resolve()
    env_file = Path(os.getenv("VOIP_APP_ENV_FILE") or (repo_root / ".env"))
    if not env_file.is_absolute():
        env_file = (repo_root / env_file).resolve()
    if not env_file.is_file():
        return 2, {"verdict": "INCONCLUSIVE", "reason": "DEPLOYMENT_ENV_FILE_NOT_FOUND"}

    env_values = _read_env(env_file)
    if str(env_values.get("APP_ENV") or "development").lower() == "production":
        return 1, {"verdict": "FAIL", "reason": "PRODUCTION_HOST_MATERIALIZATION_FORBIDDEN"}
    if str(env_values.get("CAPTURE_V2_PRODUCTION_ENABLED") or "false").strip().lower() not in {"", "0", "false", "no", "off"}:
        return 1, {"verdict": "FAIL", "reason": "PRODUCTION_V2_MUST_REMAIN_DISABLED"}

    started_temp = False
    cid = ""
    docker = ""
    compose: list[str] = []
    payload: dict[str, Any] = {
        "scope": "R6_BOUNDED_POSTGRES_PRODUCT_MATERIALIZATION",
        "production_mutation": False,
        "dut_mutation": False,
        "temporary_postgres_started": False,
        "prestate_application_services_running": False,
    }
    try:
        docker, compose = _docker_tools(repo_root)
        running = _running_postgres_containers(docker, repo_root)
        if len(running) > 1:
            raise RuntimeError(f"MULTIPLE_RUNNING_POSTGRES_CONTAINERS:{len(running)}")
        if running:
            cid = running[0]
            _wait_postgres_healthy(docker, repo_root, cid)
            payload["postgres_source"] = "EXISTING_RUNNING_CONTAINER"
        else:
            cid = _start_temp_postgres(repo_root, env_file, compose, docker)
            started_temp = True
            payload["temporary_postgres_started"] = True
            payload["postgres_source"] = "TEMPORARY_PERSISTENT_DATA_RUNTIME"

        ip = _container_ip(docker, repo_root, cid)
        child_env = os.environ.copy()
        child_env["DATABASE_URL"] = _database_url_for_ip(env_values, ip)
        child_env["CAPTURE_ENGINE_VERSION"] = "V1"
        child_env["CAPTURE_V2_PRODUCTION_ENABLED"] = "false"
        child_env["PYTHONPATH"] = "."
        cp = _run(
            [
                sys.executable, "-m", "app.capture_v2.control.r6_report_materialize",
                "--repo-root", str(repo_root),
                "--golden-path", str(golden_path),
            ],
            cwd=repo_root / "backend",
            env=child_env,
            timeout=480,
            check=False,
        )
        try:
            child = json.loads(cp.stdout.strip().splitlines()[-1] if cp.stdout.strip() else "{}")
        except Exception:
            # The materializer emits pretty JSON; parse the full stdout first.
            try:
                child = json.loads(cp.stdout)
            except Exception as exc:
                raise RuntimeError(
                    f"R6_MATERIALIZER_OUTPUT_INVALID:{type(exc).__name__}:{_safe_error(cp.stdout)}"
                ) from exc
        payload["materialization"] = child
        payload["materializer_return_code"] = cp.returncode
        if cp.returncode != 0 or child.get("verdict") != "PASS":
            payload["verdict"] = str(child.get("verdict") or "FAIL")
            payload["reason"] = str(child.get("reason") or "R6_PRODUCT_MATERIALIZATION_FAILED")
            return (1 if cp.returncode else 2), payload
        payload["verdict"] = "PASS"
        payload["reason"] = "R6_PRODUCT_MATERIALIZATION_WITH_PERSISTENT_DB_PROVEN"
        return 0, payload
    except Exception as exc:
        payload["verdict"] = "FAIL"
        payload["reason"] = "R6_PRODUCT_MATERIALIZATION_RUNTIME_EXCEPTION"
        payload["error"] = f"{type(exc).__name__}:{_safe_error(str(exc))}"
        return 1, payload
    finally:
        if started_temp and compose:
            try:
                _stop_temp_postgres(repo_root, env_file, compose)
                payload["temporary_postgres_restored_to_absent"] = True
            except Exception as exc:
                payload["temporary_postgres_restored_to_absent"] = False
                payload["cleanup_error"] = f"{type(exc).__name__}:{_safe_error(str(exc))}"
                payload["verdict"] = "FAIL"
                payload["reason"] = "R6_TEMP_POSTGRES_CLEANUP_NOT_PROVEN"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Guarded R6 product materialization with temporary persistent PostgreSQL")
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--golden-path", type=Path, required=True)
    args = parser.parse_args(argv)
    rc, payload = run(repo_root=args.repo_root, golden_path=args.golden_path)
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
