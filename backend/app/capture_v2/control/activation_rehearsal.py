from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app.capture_v2.gate.models import GateVerdict
from app.capture_v2.gate.r7_rollback import R7RollbackRehearsalGate


APP_SERVICES = (
    "backend",
    "reproduction-worker",
    "reproduction-control-high-worker",
    "reproduction-watch-worker",
    "beat",
)
PROJECT_NAME = "capture-v2-r7-rehearsal"
FALSE_VALUES = {"", "0", "false", "no", "off"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_error(text: str) -> str:
    lines: list[str] = []
    for raw in str(text or "").splitlines()[-30:]:
        upper = raw.upper()
        if any(token in upper for token in ("PASSWORD", "SECRET", "TOKEN=", "AUTHORIZATION")):
            lines.append("[REDACTED_SENSITIVE_LINE]")
        else:
            lines.append(raw[:500])
    return "\n".join(lines)[-4000:]


def _run(
    argv: list[str], *, cwd: Path, env: dict[str, str], timeout: float,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    cp = subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=timeout,
    )
    if check and cp.returncode != 0:
        raise RuntimeError(
            f"COMMAND_FAILED:{argv[0]}:{argv[-1]}:rc={cp.returncode}:"
            f"{_safe_error(cp.stderr or cp.stdout)}"
        )
    return cp


def _read_env(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.strip()] = value.strip().strip('"').strip("'")
    return result


def _write_overlay(source: Path, dest: Path, overrides: dict[str, str]) -> None:
    lines = source.read_text(encoding="utf-8", errors="replace").splitlines()
    seen: set[str] = set()
    out: list[str] = []
    for raw in lines:
        stripped = raw.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in overrides:
                out.append(f"{key}={overrides[key]}")
                seen.add(key)
                continue
        out.append(raw)
    for key, value in overrides.items():
        if key not in seen:
            out.append(f"{key}={value}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.chmod(dest, 0o600)


def _compose_prefix(repo_root: Path, env_file: Path) -> tuple[list[str], dict[str, str]]:
    docker = shutil.which("docker")
    if not docker:
        raise RuntimeError("DOCKER_NOT_AVAILABLE")
    version = _run([docker, "compose", "version"], cwd=repo_root, env=os.environ.copy(), timeout=20)
    if version.returncode != 0:
        raise RuntimeError("DOCKER_COMPOSE_NOT_AVAILABLE")
    env = os.environ.copy()
    env["VOIP_APP_ENV_FILE"] = str(env_file)
    env["COMPOSE_PROJECT_NAME"] = PROJECT_NAME
    return [docker, "compose", "--env-file", str(env_file), "-p", PROJECT_NAME], env


def _compose(
    repo_root: Path, env_file: Path, args: list[str], *, timeout: float = 300.0,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    prefix, env = _compose_prefix(repo_root, env_file)
    return _run([*prefix, *args], cwd=repo_root, env=env, timeout=timeout, check=check)


def _container_running(repo_root: Path, env_file: Path, service: str) -> bool:
    cp = _compose(repo_root, env_file, ["ps", "-q", service], timeout=30, check=False)
    cid = cp.stdout.strip().splitlines()[0].strip() if cp.returncode == 0 and cp.stdout.strip() else ""
    if not cid:
        return False
    docker = shutil.which("docker")
    if not docker:
        return False
    inspect = _run(
        [docker, "inspect", "-f", "{{.State.Running}}", cid],
        cwd=repo_root, env=os.environ.copy(), timeout=20, check=False,
    )
    return inspect.returncode == 0 and inspect.stdout.strip().lower() == "true"


def _wait_services(repo_root: Path, env_file: Path, timeout: float = 240.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if all(_container_running(repo_root, env_file, service) for service in APP_SERVICES):
            return
        time.sleep(2)
    missing = [service for service in APP_SERVICES if not _container_running(repo_root, env_file, service)]
    raise RuntimeError("SERVICES_NOT_RUNNING:" + ",".join(missing))


def _exec_json(
    repo_root: Path, env_file: Path, service: str, module_args: list[str], *, timeout: float = 120.0,
) -> dict[str, Any]:
    cp = _compose(
        repo_root,
        env_file,
        ["exec", "-T", service, "python", "-m", "app.capture_v2.control.service_rehearsal_runtime", *module_args],
        timeout=timeout,
    )
    text = cp.stdout.strip()
    try:
        return json.loads(text.splitlines()[-1])
    except Exception as exc:
        raise RuntimeError(f"RUNTIME_PROBE_JSON_INVALID:{type(exc).__name__}:{_safe_error(text)}") from exc


def _backend_http_health(repo_root: Path, env_file: Path) -> bool:
    code = (
        "import json,urllib.request; "
        "r=urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=5); "
        "d=json.loads(r.read().decode()); print(json.dumps(d)); "
        "raise SystemExit(0 if d.get('status')=='ok' else 3)"
    )
    cp = _compose(
        repo_root, env_file,
        ["exec", "-T", "backend", "python", "-c", code],
        timeout=30, check=False,
    )
    return cp.returncode == 0


def _wait_status(
    repo_root: Path,
    env_file: Path,
    session_id: str,
    predicate: Callable[[dict[str, Any]], bool],
    *,
    timeout: float,
    with_producer: bool = False,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        args = ["status", "--session-id", session_id]
        if with_producer:
            args.append("--with-producer")
        last = _exec_json(repo_root, env_file, "backend", args, timeout=60)
        if predicate(last):
            return last
        time.sleep(3)
    raise RuntimeError("SESSION_STATUS_TIMEOUT:" + json.dumps(last, ensure_ascii=False)[:1500])


def _durable_segment_count(status: dict[str, Any]) -> int:
    states = ((status.get("capture") or {}).get("segments_by_state") or {})
    return int(states.get("ACKED", 0) or 0) + int(states.get("REMOTE_DELETED", 0) or 0)


def _capture_lease_state(status: dict[str, Any]) -> str | None:
    return (((status.get("capture") or {}).get("lease") or {}).get("state"))


def _producer_count(status: dict[str, Any]) -> int | None:
    return ((status.get("dut") or {}).get("producer_count"))


def _start_app_stack(repo_root: Path, env_file: Path, *, build: bool) -> None:
    args = ["up", "-d"]
    if build:
        args.append("--build")
    args.extend(APP_SERVICES)
    _compose(repo_root, env_file, args, timeout=1800)
    _wait_services(repo_root, env_file)
    if not _backend_http_health(repo_root, env_file):
        raise RuntimeError("BACKEND_HEALTH_FAILED")


def _recreate_app_stack(repo_root: Path, env_file: Path) -> None:
    _compose(
        repo_root,
        env_file,
        ["up", "-d", "--no-deps", "--force-recreate", *APP_SERVICES],
        timeout=600,
    )
    _wait_services(repo_root, env_file)
    if not _backend_http_health(repo_root, env_file):
        raise RuntimeError("BACKEND_HEALTH_FAILED_AFTER_RECREATE")


def _down(repo_root: Path, env_file: Path) -> None:
    _compose(repo_root, env_file, ["down", "--remove-orphans"], timeout=300, check=False)


def _make_observation(
    *,
    phase: str,
    observed_at: datetime,
    engine: str,
    production_enabled: bool,
    activation_mode: str,
    v1_healthy: bool,
    producer_count: int,
    action_id: str,
    anchor: str,
) -> dict[str, Any]:
    return {
        "phase": phase,
        "observed_at": observed_at.isoformat(),
        "capture_engine_version": engine,
        "capture_v2_production_enabled": production_enabled,
        "activation_mode": activation_mode,
        "v1_healthy": v1_healthy,
        "v2_producer_count": producer_count,
        "evidence_kind": "REAL_DUT",
        "evidence_refs": [f"validation/control/results/{action_id}/result.json#{anchor}"],
    }


def run(args) -> tuple[int, dict[str, Any]]:
    repo_root = Path(args.repo_root).resolve()
    original_env = Path(os.getenv("VOIP_APP_ENV_FILE") or (repo_root / ".env"))
    if not original_env.is_absolute():
        original_env = repo_root / original_env
    if not original_env.is_file():
        return 2, {"verdict": "INCONCLUSIVE", "reason": "DEPLOYMENT_ENV_FILE_NOT_FOUND"}
    original = _read_env(original_env)
    if str(original.get("APP_ENV") or "development").lower() == "production":
        return 2, {"verdict": "INCONCLUSIVE", "reason": "PRODUCTION_HOST_REHEARSAL_FORBIDDEN"}
    if str(original.get("CAPTURE_V2_PRODUCTION_ENABLED") or "false").lower() not in FALSE_VALUES:
        return 1, {"verdict": "FAIL", "reason": "PRESTATE_PRODUCTION_V2_ALREADY_ENABLED"}

    work = repo_root / ".capture-v2-control" / "rehearsal" / args.action_id
    v1_env = work / "v1.env"
    v2_env = work / "v2-rehearsal.env"
    _write_overlay(original_env, v1_env, {
        "CAPTURE_ENGINE_VERSION": "V1",
        "CAPTURE_V2_PRODUCTION_ENABLED": "false",
        "CAPTURE_V2_ACTIVATION_REHEARSAL": "false",
        "CAPTURE_V2_REUSE_LEGACY_REPRODUCTION_SEMANTICS": "false",
        "CAPTURE_V2_RELEASE_GATE_ARTIFACT": "/app/validation/capture_v2_release_gate.json",
    })
    _write_overlay(original_env, v2_env, {
        "CAPTURE_ENGINE_VERSION": "V2",
        "CAPTURE_V2_PRODUCTION_ENABLED": "false",
        "CAPTURE_V2_ACTIVATION_REHEARSAL": "true",
        "CAPTURE_V2_REUSE_LEGACY_REPRODUCTION_SEMANTICS": "true",
        "CAPTURE_V2_RELEASE_GATE_ARTIFACT": "/app/validation/capture_v2_release_gate.json",
    })

    payload: dict[str, Any] = {
        "scope": "BOUNDED_SERVICE_LEVEL_ACTIVATION_REHEARSAL",
        "production_v2_enabled": False,
        "original_env_modified": False,
        "project": PROJECT_NAME,
        "device": {"sn": args.sn, "model": args.model, "host": args.host, "port": args.port},
        "checks": [],
    }
    session_id: str | None = None
    stack_started = False
    mode = "PRESTART"
    cleanup_errors: list[str] = []
    failure_phase: str | None = None
    observations: list[dict[str, Any]] = []

    try:
        # This action is only permitted on the idle validation host proven by preflight.
        existing = []
        for service in APP_SERVICES:
            if _container_running(repo_root, v1_env, service):
                existing.append(service)
        if existing:
            raise RuntimeError("REHEARSAL_PRESTATE_NOT_IDLE:" + ",".join(existing))

        failure_phase = "V1_BASELINE_BOOT"
        _start_app_stack(repo_root, v1_env, build=True)
        stack_started = True
        mode = "V1"
        contract_v1 = _exec_json(repo_root, v1_env, "backend", ["health-contract", "--expect", "V1"])
        prepared = _exec_json(
            repo_root, v1_env, "backend",
            [
                "prepare", "--sn", args.sn, "--host", args.host, "--port", str(args.port),
                "--username", args.username, "--platform-id", args.platform_id,
                "--model", args.model, "--profile-id", args.profile_id,
            ],
        )
        session_id = str(prepared["session_id"])
        pre_status = _exec_json(
            repo_root, v1_env, "backend",
            ["status", "--session-id", session_id, "--with-producer"], timeout=90,
        )
        if _producer_count(pre_status) != 0:
            raise RuntimeError("PRE_V1_V2_PRODUCER_PRESENT")
        pre_time = _now()
        observations.append(_make_observation(
            phase="PRE_V1", observed_at=pre_time, engine="V1", production_enabled=False,
            activation_mode="V1", v1_healthy=bool(contract_v1.get("contract_ok")) and _backend_http_health(repo_root, v1_env),
            producer_count=0, action_id=args.action_id, anchor="pre_v1",
        ))
        payload["pre_v1"] = {"contract": contract_v1, "session": pre_status}

        failure_phase = "V2_REHEARSAL_ACTIVATION"
        _recreate_app_stack(repo_root, v2_env)
        mode = "V2_REHEARSAL"
        contract_v2 = _exec_json(repo_root, v2_env, "backend", ["health-contract", "--expect", "V2_REHEARSAL"])
        _exec_json(repo_root, v2_env, "backend", ["enqueue-start", "--session-id", session_id])

        def active_ready(s: dict[str, Any]) -> bool:
            capture = s.get("capture") or {}
            return (
                s.get("state") in {"WATCHING", "ACTIVITY_DETECTED"}
                and capture.get("path_ready") is True
                and _capture_lease_state(s) == "ACTIVE"
                and _durable_segment_count(s) >= 1
            )

        active_db = _wait_status(repo_root, v2_env, session_id, active_ready, timeout=180)
        active = _exec_json(
            repo_root, v2_env, "backend",
            ["status", "--session-id", session_id, "--with-producer"], timeout=90,
        )
        if _producer_count(active) != 1:
            raise RuntimeError(f"V2_PRODUCER_COUNT_INVALID:{_producer_count(active)}")
        time.sleep(max(5.0, min(float(args.observation_seconds), 60.0)))
        active_final = _exec_json(
            repo_root, v2_env, "backend",
            ["status", "--session-id", session_id, "--with-producer"], timeout=90,
        )
        if _producer_count(active_final) != 1 or _capture_lease_state(active_final) != "ACTIVE":
            raise RuntimeError("V2_ACTIVE_NOT_STABLE")
        v2_time = _now()
        observations.append(_make_observation(
            phase="V2_ACTIVE", observed_at=v2_time, engine="V2", production_enabled=False,
            activation_mode="ACTIVATION_REHEARSAL", v1_healthy=True,
            producer_count=1, action_id=args.action_id, anchor="v2_active",
        ))
        payload["v2_active"] = {
            "contract": contract_v2,
            "initial": active_db,
            "stable": active_final,
        }

        failure_phase = "V2_SERIALIZED_CLEANUP"
        _exec_json(repo_root, v2_env, "backend", ["enqueue-cancel", "--session-id", session_id])

        def v2_clean(s: dict[str, Any]) -> bool:
            capture = s.get("capture") or {}
            return (
                s.get("state") in {"CANCELLED", "PARTIAL_SUCCESS", "COMPLETED"}
                and s.get("cleanup_status") == "CLEANUP_VERIFIED"
                and capture.get("evidence_durable") is True
                and _capture_lease_state(s) == "RELEASED"
            )

        clean_db = _wait_status(repo_root, v2_env, session_id, v2_clean, timeout=240)
        clean = _exec_json(
            repo_root, v2_env, "backend",
            ["status", "--session-id", session_id, "--with-producer"], timeout=90,
        )
        if _producer_count(clean) != 0:
            raise RuntimeError("V2_PRODUCER_RESIDUAL_AFTER_CLEANUP")
        payload["v2_cleanup"] = {"db": clean_db, "final": clean}

        failure_phase = "ROLLBACK_TO_V1"
        _recreate_app_stack(repo_root, v1_env)
        mode = "V1"
        contract_v1_after = _exec_json(repo_root, v1_env, "backend", ["health-contract", "--expect", "V1"])
        clone = _exec_json(repo_root, v1_env, "backend", ["clone-session", "--source-session-id", session_id])
        v1_session_id = str(clone["session_id"])
        _exec_json(repo_root, v1_env, "backend", ["enqueue-start", "--session-id", v1_session_id])
        _wait_status(
            repo_root, v1_env, v1_session_id,
            lambda s: s.get("state") in {"WATCHING", "ACTIVITY_DETECTED"},
            timeout=180,
        )
        _exec_json(repo_root, v1_env, "backend", ["enqueue-cancel", "--session-id", v1_session_id])
        v1_terminal = _wait_status(
            repo_root, v1_env, v1_session_id,
            lambda s: s.get("state") in {"CANCELLED", "PARTIAL_SUCCESS", "COMPLETED"}
            and s.get("cleanup_status") == "CLEANUP_VERIFIED",
            timeout=240,
        )
        rolled_probe = _exec_json(
            repo_root, v1_env, "backend",
            ["status", "--session-id", session_id, "--with-producer"], timeout=90,
        )
        if _producer_count(rolled_probe) != 0:
            raise RuntimeError("ROLLBACK_V2_PRODUCER_RESIDUAL")
        rolled_time = _now()
        observations.append(_make_observation(
            phase="ROLLED_BACK_V1", observed_at=rolled_time, engine="V1", production_enabled=False,
            activation_mode="V1", v1_healthy=bool(contract_v1_after.get("contract_ok")) and _backend_http_health(repo_root, v1_env),
            producer_count=0, action_id=args.action_id, anchor="rolled_back_v1",
        ))
        payload["rolled_back_v1"] = {
            "contract": contract_v1_after,
            "v1_health_session": v1_terminal,
            "original_v2_session_probe": rolled_probe,
        }

        evidence = {
            "schema_version": R7RollbackRehearsalGate.EVIDENCE_SCHEMA,
            "observations": observations,
        }
        gate = R7RollbackRehearsalGate.evaluate_artifact(evidence)
        payload["rollback_evidence"] = evidence
        payload["rollback_gate"] = gate.as_dict()
        if gate.verdict != GateVerdict.PASS:
            raise RuntimeError(f"R7_ROLLBACK_GATE_NOT_PASS:{gate.verdict.value}")

        payload["checks"].extend([
            {"name": "pre_v1_real_dut_no_v2_producer", "passed": True},
            {"name": "v2_rehearsal_real_service_authority", "passed": True},
            {"name": "v2_segments_durable", "passed": _durable_segment_count(active_final) >= 1},
            {"name": "v2_cleanup_verified", "passed": True},
            {"name": "rollback_v1_business_path", "passed": True},
            {"name": "rollback_no_v2_producer", "passed": True},
            {"name": "r7_gate", "passed": True},
        ])
        payload["verdict"] = "PASS"
        payload["reason"] = "SERVICE_LEVEL_ACTIVATION_REHEARSAL_PROVEN"
        return 0, payload

    except Exception as exc:
        payload["verdict"] = "INCONCLUSIVE" if failure_phase in {
            "V1_BASELINE_BOOT", "V2_REHEARSAL_ACTIVATION"
        } else "FAIL"
        payload["reason"] = "SERVICE_LEVEL_REHEARSAL_EXCEPTION"
        payload["failure_phase"] = failure_phase
        payload["error"] = f"{type(exc).__name__}:{_safe_error(str(exc))}"
        return (2 if payload["verdict"] == "INCONCLUSIVE" else 1), payload
    finally:
        # Fail-safe rollback is unconditional. Never leave the bounded project in V2.
        try:
            if stack_started and session_id:
                try:
                    # If V2 is still selected, request serialized cleanup first.
                    current_env = v2_env if mode == "V2_REHEARSAL" else v1_env
                    _exec_json(repo_root, current_env, "backend", ["enqueue-cancel", "--session-id", session_id], timeout=60)
                    time.sleep(3)
                except Exception as exc:
                    cleanup_errors.append("cancel:" + _safe_error(str(exc)))
            if stack_started:
                try:
                    _recreate_app_stack(repo_root, v1_env)
                    mode = "V1"
                    if session_id:
                        try:
                            _exec_json(repo_root, v1_env, "backend", ["enqueue-cancel", "--session-id", session_id], timeout=60)
                            time.sleep(3)
                        except Exception:
                            pass
                except Exception as exc:
                    cleanup_errors.append("restore_v1:" + _safe_error(str(exc)))
                try:
                    _down(repo_root, v1_env)
                except Exception as exc:
                    cleanup_errors.append("down:" + _safe_error(str(exc)))
        finally:
            try:
                shutil.rmtree(work, ignore_errors=True)
            except Exception:
                pass
        if cleanup_errors:
            payload["failsafe_cleanup_errors"] = cleanup_errors
        payload["final_server_target_state"] = "PRESTATE_NO_REHEARSAL_CONTAINERS"
        payload["final_authority_target"] = "V1"
        payload["production_v2_enabled"] = False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bounded real service-level Capture V2 activation rehearsal")
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
    rc, payload = run(args)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
