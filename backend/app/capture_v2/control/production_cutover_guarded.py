from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.capture_v2.control.production_deployment_preflight_guarded import (
    AUTH_RELATIVE,
    EXPECTED_FINAL_ACTION,
    PRODUCTION_ENV,
    RELEASE_GATE_RELATIVE,
    REQUIRED_RELEASE_TRUE,
    _load_json,
    _safe_tail,
    _validate_authorization,
    _validate_release_gate,
)


PROJECT = "voip-ai"
COMPOSE_FILES = ("docker-compose.yml", "docker-compose.production.yml")
REQUIRED_APP_SERVICES = (
    "backend",
    "collector-worker",
    "packet-worker",
    "pcm-worker",
    "media-worker",
    "diagnosis-worker",
    "reproduction-worker",
    "reproduction-control-high-worker",
    "reproduction-watch-worker",
    "beat",
    "frontend",
)
SAFE_ENV_KEYS = (
    "APP_ENV",
    "BUILD_REVISION",
    "REPRODUCTION_PLATFORM_MODE",
    "CAPTURE_ENGINE_VERSION",
    "CAPTURE_V2_PRODUCTION_ENABLED",
    "CAPTURE_V2_ACTIVATION_REHEARSAL",
    "CAPTURE_V2_REUSE_LEGACY_REPRODUCTION_SEMANTICS",
    "CAPTURE_V2_RELEASE_GATE_ARTIFACT",
    "VOIP_PROJECT_NAME",
    "VOIP_DATA_ROOT",
)


def _run(argv: list[str], *, cwd: Path, timeout: float = 600.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


def _git(repo_root: Path, *args: str, timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
    return _run(["git", *args], cwd=repo_root, timeout=timeout)


def _sudo(*argv: str, cwd: Path, timeout: float = 600.0) -> subprocess.CompletedProcess[str]:
    return _run(["/usr/bin/sudo", "-n", *argv], cwd=cwd, timeout=timeout)


def _compose_args(env_file: Path, *args: str) -> list[str]:
    out = [
        "/usr/bin/docker", "compose",
        "--project-name", PROJECT,
        "--env-file", str(env_file),
    ]
    for compose_file in COMPOSE_FILES:
        out.extend(["-f", compose_file])
    out.extend(args)
    return out


def _read_safe_env(repo_root: Path) -> tuple[int, dict[str, str], str]:
    code = r'''
import json
from pathlib import Path
p=Path("/etc/voip-ai/production.env")
keys=set(__import__("os").environ["SAFE_KEYS"].split(","))
out={}
for raw in p.read_text(encoding="utf-8").splitlines():
    line=raw.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    if line.startswith("export "):
        line=line[7:].strip()
    k,v=line.split("=",1); k=k.strip()
    if k not in keys:
        continue
    v=v.strip()
    if len(v)>=2 and v[0]==v[-1] and v[0] in {"'", '"'}:
        v=v[1:-1]
    out[k]=v
print(json.dumps(out,sort_keys=True))
'''
    cp = _sudo(
        "/usr/bin/env", f"SAFE_KEYS={','.join(SAFE_ENV_KEYS)}",
        "/usr/bin/python3", "-c", code,
        cwd=repo_root, timeout=60,
    )
    if cp.returncode != 0:
        return cp.returncode, {}, _safe_tail(cp.stderr or cp.stdout)
    try:
        return 0, json.loads(cp.stdout.strip()), ""
    except Exception as exc:
        return 3, {}, f"SAFE_ENV_JSON_INVALID:{type(exc).__name__}"


def _write_cutover_env(repo_root: Path, backup_path: Path) -> tuple[int, dict[str, Any], str]:
    code = r'''
import json, os, shutil, stat
from pathlib import Path
src=Path("/etc/voip-ai/production.env")
backup=Path(os.environ["BACKUP_PATH"])
overrides={
 "REPRODUCTION_PLATFORM_MODE":"real",
 "CAPTURE_ENGINE_VERSION":"V2",
 "CAPTURE_V2_PRODUCTION_ENABLED":"true",
 "CAPTURE_V2_ACTIVATION_REHEARSAL":"false",
 "CAPTURE_V2_REUSE_LEGACY_REPRODUCTION_SEMANTICS":"false",
 "CAPTURE_V2_RELEASE_GATE_ARTIFACT":"/app/validation/capture_v2_release_gate.json",
}
mode=stat.S_IMODE(src.stat().st_mode)
if mode & 0o077:
    raise SystemExit("PRODUCTION_ENV_PERMISSIONS_NOT_PRIVATE")
shutil.copy2(src,backup)
os.chmod(backup,0o600)
lines=src.read_text(encoding="utf-8").splitlines()
out=[]; seen=set()
for raw in lines:
    stripped=raw.strip()
    if stripped and not stripped.startswith("#") and "=" in stripped:
        probe=stripped[7:].strip() if stripped.startswith("export ") else stripped
        key=probe.split("=",1)[0].strip()
        if key in overrides:
            prefix="export " if stripped.startswith("export ") else ""
            out.append(f"{prefix}{key}={overrides[key]}")
            seen.add(key); continue
    out.append(raw)
for key,value in overrides.items():
    if key not in seen:
        out.append(f"{key}={value}")
tmp=src.with_name(src.name+".capture-v2-new")
tmp.write_text("\n".join(out)+"\n",encoding="utf-8")
os.chmod(tmp,0o600)
os.replace(tmp,src)
print(json.dumps({"backup":str(backup),"overrides":overrides},sort_keys=True))
'''
    cp = _sudo(
        "/usr/bin/env", f"BACKUP_PATH={backup_path}",
        "/usr/bin/python3", "-c", code,
        cwd=repo_root, timeout=60,
    )
    if cp.returncode != 0:
        return cp.returncode, {}, _safe_tail(cp.stderr or cp.stdout)
    try:
        return 0, json.loads(cp.stdout.strip()), ""
    except Exception as exc:
        return 3, {}, f"CUTOVER_ENV_JSON_INVALID:{type(exc).__name__}"


def _restore_env(repo_root: Path, backup_path: Path) -> tuple[bool, str]:
    code = r'''
import os, shutil
from pathlib import Path
src=Path(os.environ["BACKUP_PATH"]); dst=Path("/etc/voip-ai/production.env")
if not src.is_file(): raise SystemExit("BACKUP_MISSING")
shutil.copy2(src,dst); os.chmod(dst,0o600)
'''
    cp = _sudo(
        "/usr/bin/env", f"BACKUP_PATH={backup_path}",
        "/usr/bin/python3", "-c", code,
        cwd=repo_root, timeout=60,
    )
    return cp.returncode == 0, _safe_tail(cp.stderr or cp.stdout)


def _running_services(repo_root: Path) -> tuple[int, list[str], str]:
    cp = _sudo(*_compose_args(PRODUCTION_ENV, "ps", "--status", "running", "--services"), cwd=repo_root, timeout=60)
    return cp.returncode, sorted({x.strip() for x in cp.stdout.splitlines() if x.strip()}), _safe_tail(cp.stderr)


def _release_preflight(repo_root: Path) -> tuple[int, dict[str, Any], str]:
    out = Path(tempfile.gettempdir()) / f"capture-v2-release-preflight-{os.getpid()}.json"
    try:
        cp = _sudo(
            "/usr/bin/python3", str(repo_root / "deploy/deployment_preflight.py"),
            "--env-file", str(PRODUCTION_ENV),
            "--mode", "release",
            "--out", str(out),
            cwd=repo_root, timeout=180,
        )
        report = _load_json(out) if out.is_file() else {}
        return cp.returncode, report, _safe_tail(cp.stderr or cp.stdout)
    finally:
        try:
            out.unlink(missing_ok=True)
        except Exception:
            pass


def _deployment(repo_root: Path) -> subprocess.CompletedProcess[str]:
    return _sudo(
        str(repo_root / "deploy/voip-ai"),
        "--env", str(PRODUCTION_ENV),
        "--project", PROJECT,
        "deploy",
        cwd=repo_root,
        timeout=5400,
    )


def _runtime_verify(repo_root: Path) -> subprocess.CompletedProcess[str]:
    return _sudo(
        str(repo_root / "deploy/voip-ai"),
        "--env", str(PRODUCTION_ENV),
        "--project", PROJECT,
        "verify",
        cwd=repo_root,
        timeout=900,
    )


def _backend_authority(repo_root: Path) -> tuple[int, dict[str, Any], str]:
    code = (
        "import json; "
        "from app.capture_v2.runtime import capture_authority_mode; "
        "from app.core.config import settings; "
        "print(json.dumps({'engine':str(settings.capture_engine_version),'production_enabled':bool(settings.capture_v2_production_enabled),'platform':str(settings.reproduction_platform_mode),'authority':capture_authority_mode()}))"
    )
    cp = _sudo(
        *_compose_args(PRODUCTION_ENV, "exec", "-T", "backend", "python", "-c", code),
        cwd=repo_root, timeout=90,
    )
    if cp.returncode != 0:
        return cp.returncode, {}, _safe_tail(cp.stderr or cp.stdout)
    try:
        return 0, json.loads(cp.stdout.strip().splitlines()[-1]), ""
    except Exception as exc:
        return 3, {}, f"AUTHORITY_JSON_INVALID:{type(exc).__name__}"


def _rollback(repo_root: Path, *, backup_path: Path, pre_running: list[str]) -> dict[str, Any]:
    result: dict[str, Any] = {"attempted": True, "env_restored": False, "runtime_restored": False}
    ok, err = _restore_env(repo_root, backup_path)
    result["env_restored"] = ok
    if err:
        result["env_restore_error"] = err
    if not ok:
        return result

    if not pre_running:
        down = _sudo(*_compose_args(PRODUCTION_ENV, "down", "--remove-orphans"), cwd=repo_root, timeout=600)
        result["rollback_command"] = "compose down --remove-orphans"
        result["rollback_return_code"] = down.returncode
        result["runtime_restored"] = down.returncode == 0
        if down.returncode != 0:
            result["rollback_error"] = _safe_tail(down.stderr or down.stdout)
        return result

    # If there was an existing production stack, recreate the exact previously
    # running services under the restored environment rather than inventing a new
    # service set.
    up = _sudo(
        *_compose_args(PRODUCTION_ENV, "up", "-d", "--force-recreate", *pre_running),
        cwd=repo_root, timeout=1800,
    )
    result["rollback_command"] = "compose up --force-recreate <pre-running-services>"
    result["rollback_return_code"] = up.returncode
    result["runtime_restored"] = up.returncode == 0
    if up.returncode != 0:
        result["rollback_error"] = _safe_tail(up.stderr or up.stdout)
    return result


def run(*, repo_root: Path, authorization_path: Path) -> tuple[int, dict[str, Any]]:
    repo_root = repo_root.resolve()
    payload: dict[str, Any] = {
        "scope": "GUARDED_PRODUCTION_CAPTURE_V2_CUTOVER",
        "project": PROJECT,
        "production_env": str(PRODUCTION_ENV),
        "production_mutation": False,
        "dut_mutation": False,
        "rollback_on_failure": True,
        "stages": {},
    }

    ok, reason, auth = _validate_authorization(repo_root, authorization_path)
    if not ok:
        return 1, {**payload, "verdict": "FAIL", "reason": reason}
    if auth.get("cutover_ready") is not True:
        return 1, {**payload, "verdict": "FAIL", "reason": "AUTHORIZATION_NOT_MARKED_CUTOVER_READY"}
    validated_head = str(auth.get("validated_feature_head") or "").strip()
    if len(validated_head) != 40:
        return 1, {**payload, "verdict": "FAIL", "reason": "VALIDATED_FEATURE_HEAD_MISSING"}

    ok, reason, gate = _validate_release_gate(repo_root)
    if not ok:
        return 1, {**payload, "verdict": "FAIL", "reason": reason}

    fetch = _git(repo_root, "fetch", "origin", "master")
    if fetch.returncode != 0:
        return 2, {**payload, "verdict": "INCONCLUSIVE", "reason": "MASTER_FETCH_FAILED", "error": _safe_tail(fetch.stderr)}
    ancestor = _git(repo_root, "merge-base", "--is-ancestor", validated_head, "origin/master")
    payload["stages"]["validated_source_merged"] = {"passed": ancestor.returncode == 0, "validated_feature_head": validated_head}
    if ancestor.returncode != 0:
        return 1, {**payload, "verdict": "FAIL", "reason": "VALIDATED_FEATURE_HEAD_NOT_MERGED_TO_MASTER"}

    rc, safe_env, error = _read_safe_env(repo_root)
    payload["pre_env"] = safe_env
    if rc != 0:
        return 2, {**payload, "verdict": "INCONCLUSIVE", "reason": "PRODUCTION_ENV_SAFE_READ_FAILED", "error": error}
    expected_pre = {
        "APP_ENV": "production",
        "CAPTURE_ENGINE_VERSION": "V1",
        "CAPTURE_V2_PRODUCTION_ENABLED": "false",
        "CAPTURE_V2_ACTIVATION_REHEARSAL": "false",
    }
    bad_pre = {key: {"expected": value, "observed": safe_env.get(key)} for key, value in expected_pre.items() if str(safe_env.get(key) or "").lower() != value.lower()}
    if bad_pre:
        return 1, {**payload, "verdict": "FAIL", "reason": "PRODUCTION_PRESTATE_INVALID", "prestate_mismatch": bad_pre}

    run_rc, pre_running, run_err = _running_services(repo_root)
    if run_rc != 0:
        return 2, {**payload, "verdict": "INCONCLUSIVE", "reason": "PRODUCTION_RUNNING_SERVICES_UNREADABLE", "error": run_err}
    payload["pre_running_services"] = pre_running

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = Path(f"/etc/voip-ai/production.env.capture-v2-pre-{stamp}")
    mutated = False
    success = False
    try:
        env_rc, env_change, env_err = _write_cutover_env(repo_root, backup_path)
        payload["stages"]["env_switch"] = {"return_code": env_rc, "backup_path": str(backup_path), "overrides": env_change.get("overrides") if env_change else None}
        if env_rc != 0:
            return 1, {**payload, "verdict": "FAIL", "reason": "PRODUCTION_ENV_SWITCH_FAILED", "error": env_err}
        mutated = True
        payload["production_mutation"] = True

        release_rc, release_report, release_err = _release_preflight(repo_root)
        payload["stages"]["strict_release_preflight"] = {
            "return_code": release_rc,
            "release_status": release_report.get("release_status"),
            "release_blocking_keys": release_report.get("release_blocking_keys") or [],
        }
        if release_rc != 0 or release_report.get("release_status") != "PASS":
            return 1, {**payload, "verdict": "FAIL", "reason": "POST_SWITCH_RELEASE_PREFLIGHT_FAILED", "error": release_err}

        deploy = _deployment(repo_root)
        payload["stages"]["production_deploy"] = {
            "return_code": deploy.returncode,
            "stdout_tail": _safe_tail(deploy.stdout),
            "stderr_tail": _safe_tail(deploy.stderr),
        }
        if deploy.returncode != 0:
            return 1, {**payload, "verdict": "FAIL", "reason": "PRODUCTION_DEPLOY_FAILED"}

        verify = _runtime_verify(repo_root)
        payload["stages"]["runtime_verify"] = {
            "return_code": verify.returncode,
            "stdout_tail": _safe_tail(verify.stdout),
            "stderr_tail": _safe_tail(verify.stderr),
        }
        if verify.returncode != 0:
            return 1, {**payload, "verdict": "FAIL", "reason": "PRODUCTION_RUNTIME_VERIFY_FAILED"}

        auth_rc, authority, auth_err = _backend_authority(repo_root)
        payload["stages"]["capture_authority"] = {"return_code": auth_rc, "observed": authority}
        if auth_rc != 0:
            return 1, {**payload, "verdict": "FAIL", "reason": "PRODUCTION_AUTHORITY_PROBE_FAILED", "error": auth_err}
        if not (
            str(authority.get("engine") or "").upper() == "V2"
            and authority.get("production_enabled") is True
            and str(authority.get("platform") or "").lower() == "real"
            and authority.get("authority") == "V2"
        ):
            return 1, {**payload, "verdict": "FAIL", "reason": "PRODUCTION_V2_AUTHORITY_NOT_ESTABLISHED"}

        running_rc, running, running_err = _running_services(repo_root)
        payload["post_running_services"] = running
        required = set(REQUIRED_APP_SERVICES)
        missing = sorted(required - set(running))
        payload["stages"]["required_services_running"] = {"passed": running_rc == 0 and not missing, "missing": missing}
        if running_rc != 0 or missing:
            return 1, {**payload, "verdict": "FAIL", "reason": "PRODUCTION_REQUIRED_SERVICES_NOT_RUNNING", "error": running_err}

        success = True
        payload["verdict"] = "PASS"
        payload["reason"] = "PRODUCTION_CAPTURE_V2_CUTOVER_PASS"
        payload["active_runtime"] = {
            "capture_engine_version": "V2",
            "capture_v2_production_enabled": True,
            "reproduction_platform_mode": "real",
            "authority": "V2",
        }
        payload["backup_path"] = str(backup_path)
        return 0, payload
    except subprocess.TimeoutExpired as exc:
        return 1, {**payload, "verdict": "FAIL", "reason": "PRODUCTION_CUTOVER_TIMEOUT", "error": str(exc.cmd)}
    except Exception as exc:
        return 1, {**payload, "verdict": "FAIL", "reason": "PRODUCTION_CUTOVER_EXCEPTION", "error": f"{type(exc).__name__}:{_safe_tail(str(exc))}"}
    finally:
        if mutated and not success:
            payload["rollback"] = _rollback(repo_root, backup_path=backup_path, pre_running=pre_running)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Guarded Capture V2 production cutover")
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--authorization", type=Path, required=True)
    args = parser.parse_args(argv)
    rc, payload = run(repo_root=args.repo_root, authorization_path=args.authorization)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
