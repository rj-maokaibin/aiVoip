from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any


PRODUCTION_ENV = Path("/etc/voip-ai/production.env")
AUTH_RELATIVE = Path("validation/capture_v2/PRODUCTION_CUTOVER_AUTHORIZATION_RC69.json")
RELEASE_GATE_RELATIVE = Path("validation/capture_v2_release_gate.json")
EXPECTED_FINAL_ACTION = "RC68-MASTER-FIX-CANDIDATE-INTEGRATION-005"
REQUIRED_RELEASE_TRUE = (
    "software_gate_passed",
    "real_ownership_gate_passed",
    "real_segment_gate_passed",
    "readiness_gate_passed",
    "coverage_gate_passed",
    "e2e_gate_passed",
    "rollback_gate_passed",
    "approved",
    "production_cutover_approved",
)


def _safe_tail(value: str, limit: int = 12000) -> str:
    lines: list[str] = []
    for raw in str(value or "").splitlines()[-120:]:
        upper = raw.upper()
        if any(token in upper for token in ("PASSWORD", "SECRET", "TOKEN=", "AUTHORIZATION")):
            lines.append("[REDACTED_SENSITIVE_LINE]")
        else:
            lines.append(raw[:1000])
    return "\n".join(lines)[-limit:]


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _run(argv: list[str], *, cwd: Path, timeout: float = 300.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


def _validate_authorization(repo_root: Path, supplied: Path) -> tuple[bool, str | None, dict[str, Any]]:
    expected = (repo_root / AUTH_RELATIVE).resolve()
    if supplied.resolve() != expected:
        return False, "PRODUCTION_AUTHORIZATION_PATH_NOT_AUDITED", {}
    if not expected.is_file():
        return False, "PRODUCTION_AUTHORIZATION_MISSING", {}
    try:
        auth = _load_json(expected)
    except Exception as exc:
        return False, f"PRODUCTION_AUTHORIZATION_INVALID:{type(exc).__name__}", {}
    if auth.get("authorized") is not True:
        return False, "PRODUCTION_AUTHORIZATION_FALSE", auth
    if auth.get("technical_release_validation") != "PASS":
        return False, "TECHNICAL_RELEASE_VALIDATION_NOT_PASS", auth
    if auth.get("final_acceptance_action") != EXPECTED_FINAL_ACTION:
        return False, "FINAL_ACCEPTANCE_ACTION_NOT_AUDITED", auth
    if auth.get("final_acceptance_verdict") != "PASS":
        return False, "FINAL_ACCEPTANCE_NOT_PASS", auth
    return True, None, auth


def _validate_release_gate(repo_root: Path) -> tuple[bool, str | None, dict[str, Any]]:
    path = (repo_root / RELEASE_GATE_RELATIVE).resolve()
    try:
        gate = _load_json(path)
    except Exception as exc:
        return False, f"RELEASE_GATE_INVALID:{type(exc).__name__}", {}
    if gate.get("schema_version") != "capture-v2-release-gate-v1":
        return False, "RELEASE_GATE_SCHEMA_INVALID", gate
    missing = [key for key in REQUIRED_RELEASE_TRUE if gate.get(key) is not True]
    if missing:
        return False, "RELEASE_GATE_NOT_APPROVED:" + ",".join(missing), gate
    # Approval does not itself mutate runtime authority. Preflight must happen while
    # the deployed system is still V1 and Production V2 is still disabled.
    if gate.get("capture_engine_version") != "V1" or gate.get("production_v2_enabled") is not False:
        return False, "PRE_CUTOVER_RELEASE_GATE_RUNTIME_STATE_INVALID", gate
    return True, None, gate


def run(*, repo_root: Path, authorization_path: Path) -> tuple[int, dict[str, Any]]:
    repo_root = repo_root.resolve()
    payload: dict[str, Any] = {
        "scope": "GUARDED_PRODUCTION_DEPLOYMENT_PREFLIGHT",
        "production_env": str(PRODUCTION_ENV),
        "mutations_performed": False,
        "runtime_cutover_performed": False,
    }

    ok, reason, auth = _validate_authorization(repo_root, authorization_path)
    payload["authorization"] = {
        "authorized": bool(auth.get("authorized")) if auth else False,
        "final_acceptance_action": auth.get("final_acceptance_action") if auth else None,
    }
    if not ok:
        return 1, {**payload, "verdict": "FAIL", "reason": reason}

    ok, reason, gate = _validate_release_gate(repo_root)
    payload["release_gate"] = {
        "approved": gate.get("approved") if gate else None,
        "production_cutover_approved": gate.get("production_cutover_approved") if gate else None,
        "capture_engine_version": gate.get("capture_engine_version") if gate else None,
        "production_v2_enabled": gate.get("production_v2_enabled") if gate else None,
    }
    if not ok:
        return 1, {**payload, "verdict": "FAIL", "reason": reason}

    sudo = Path("/usr/bin/sudo")
    python3 = Path("/usr/bin/python3")
    docker = Path("/usr/bin/docker")
    script = repo_root / "deploy/deployment_preflight.py"
    if not sudo.is_file() or not python3.is_file() or not docker.is_file() or not script.is_file():
        return 2, {**payload, "verdict": "INCONCLUSIVE", "reason": "PRODUCTION_PREFLIGHT_TOOLING_MISSING"}

    tmp = Path(tempfile.gettempdir()) / f"capture-v2-production-preflight-{os.getpid()}.json"
    try:
        cp = _run(
            [
                str(sudo), "-n", str(python3), str(script),
                "--env-file", str(PRODUCTION_ENV),
                "--mode", "deploy",
                "--out", str(tmp),
            ],
            cwd=repo_root,
            timeout=180,
        )
        payload["deployment_preflight"] = {
            "return_code": cp.returncode,
            "stdout_tail": _safe_tail(cp.stdout),
            "stderr_tail": _safe_tail(cp.stderr),
        }
        report: dict[str, Any] = {}
        if tmp.is_file():
            try:
                report = _load_json(tmp)
            except Exception:
                report = {}
        payload["deployment_preflight"]["status"] = report.get("status")
        payload["deployment_preflight"]["deployment_status"] = report.get("deployment_status")
        payload["deployment_preflight"]["deploy_blocking_keys"] = report.get("deploy_blocking_keys") or []
        if cp.returncode != 0 or report.get("deployment_status") != "PASS":
            return 2, {**payload, "verdict": "INCONCLUSIVE", "reason": "PRODUCTION_ENV_PREFLIGHT_BLOCKED"}

        compose = _run(
            [
                str(sudo), "-n", "env", f"VOIP_APP_ENV_FILE={PRODUCTION_ENV}",
                str(docker), "compose",
                "--project-name", "voip-ai",
                "--env-file", str(PRODUCTION_ENV),
                "-f", "docker-compose.yml",
                "-f", "docker-compose.production.yml",
                "config", "--services",
            ],
            cwd=repo_root,
            timeout=120,
        )
        services = sorted({line.strip() for line in compose.stdout.splitlines() if line.strip()})
        required = {
            "backend",
            "reproduction-worker",
            "reproduction-control-high-worker",
            "reproduction-watch-worker",
            "beat",
        }
        payload["compose_config"] = {
            "return_code": compose.returncode,
            "required_services_present": sorted(required.intersection(services)),
            "missing_required_services": sorted(required.difference(services)),
            "stderr_tail": _safe_tail(compose.stderr),
        }
        if compose.returncode != 0 or not required.issubset(set(services)):
            return 2, {**payload, "verdict": "INCONCLUSIVE", "reason": "PRODUCTION_COMPOSE_CONFIG_BLOCKED"}

        running = _run(
            [
                str(sudo), "-n", "env", f"VOIP_APP_ENV_FILE={PRODUCTION_ENV}",
                str(docker), "compose",
                "--project-name", "voip-ai",
                "--env-file", str(PRODUCTION_ENV),
                "-f", "docker-compose.yml",
                "-f", "docker-compose.production.yml",
                "ps", "--status", "running", "--services",
            ],
            cwd=repo_root,
            timeout=60,
        )
        payload["current_running_services"] = sorted(
            {line.strip() for line in running.stdout.splitlines() if line.strip()}
        ) if running.returncode == 0 else []

        payload["verdict"] = "PASS"
        payload["reason"] = "PRODUCTION_DEPLOYMENT_PREFLIGHT_READY"
        return 0, payload
    except subprocess.TimeoutExpired as exc:
        return 2, {
            **payload,
            "verdict": "INCONCLUSIVE",
            "reason": "PRODUCTION_PREFLIGHT_TIMEOUT",
            "error": str(exc.cmd),
        }
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Guarded Capture V2 production deployment preflight")
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--authorization", type=Path, required=True)
    args = parser.parse_args(argv)
    rc, payload = run(repo_root=args.repo_root, authorization_path=args.authorization)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
