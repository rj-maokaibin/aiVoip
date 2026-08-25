from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.capture_v2.control.production_deployment_preflight_guarded import (
    PRODUCTION_ENV,
    _validate_authorization,
    _validate_release_gate,
)


TARGET_KEY = "FEISHU_IDENTITY_RBAC_ENABLED"


def _parse_safe_env(path: Path) -> dict[str, str]:
    wanted = {
        "APP_ENV",
        "CAPTURE_ENGINE_VERSION",
        "CAPTURE_V2_PRODUCTION_ENABLED",
        "REPRODUCTION_PLATFORM_MODE",
        TARGET_KEY,
    }
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, value = line.split("=", 1)
        key = key.strip()
        if key not in wanted:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        out[key] = value
    return out


def _bool_false(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"", "0", "false", "no", "off"}


def _bool_true(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _write_only_target(path: Path) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    seen = False
    for raw in lines:
        stripped = raw.strip()
        probe = stripped[7:].strip() if stripped.startswith("export ") else stripped
        if probe and not probe.startswith("#") and "=" in probe:
            key = probe.split("=", 1)[0].strip()
            if key == TARGET_KEY:
                if not seen:
                    prefix = "export " if stripped.startswith("export ") else ""
                    out.append(f"{prefix}{TARGET_KEY}=true")
                    seen = True
                continue
        out.append(raw)
    if not seen:
        out.append(f"{TARGET_KEY}=true")

    tmp = path.with_name(path.name + ".feishu-rbac-new")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def run(*, repo_root: Path, authorization_path: Path) -> tuple[int, dict[str, Any]]:
    repo_root = repo_root.resolve()
    payload: dict[str, Any] = {
        "scope": "GUARDED_PRODUCTION_FEISHU_RBAC_ENABLE",
        "production_env": str(PRODUCTION_ENV),
        "target_key": TARGET_KEY,
        "target_value": True,
        "mutations_performed": False,
        "runtime_restart_performed": False,
        "dut_mutation": False,
    }

    ok, reason, auth = _validate_authorization(repo_root, authorization_path)
    payload["authorization"] = {
        "authorized": bool(auth.get("authorized")) if auth else False,
        "cutover_ready": auth.get("cutover_ready") if auth else None,
        "final_acceptance_action": auth.get("final_acceptance_action") if auth else None,
    }
    if not ok:
        return 1, {**payload, "verdict": "FAIL", "reason": reason}
    if auth.get("cutover_ready") is not True:
        return 1, {**payload, "verdict": "FAIL", "reason": "AUTHORIZATION_NOT_MARKED_CUTOVER_READY"}

    ok, reason, gate = _validate_release_gate(repo_root)
    payload["release_gate"] = {
        "approved": gate.get("approved") if gate else None,
        "production_cutover_approved": gate.get("production_cutover_approved") if gate else None,
    }
    if not ok:
        return 1, {**payload, "verdict": "FAIL", "reason": reason}

    if not PRODUCTION_ENV.is_file():
        return 2, {**payload, "verdict": "INCONCLUSIVE", "reason": "PRODUCTION_ENV_MISSING"}
    mode = stat.S_IMODE(PRODUCTION_ENV.stat().st_mode)
    if mode & 0o077:
        return 1, {**payload, "verdict": "FAIL", "reason": "PRODUCTION_ENV_PERMISSIONS_NOT_PRIVATE"}

    before = _parse_safe_env(PRODUCTION_ENV)
    payload["prestate"] = {
        "app_env": before.get("APP_ENV"),
        "capture_engine_version": before.get("CAPTURE_ENGINE_VERSION"),
        "capture_v2_production_enabled": before.get("CAPTURE_V2_PRODUCTION_ENABLED"),
        "reproduction_platform_mode": before.get("REPRODUCTION_PLATFORM_MODE"),
        "feishu_identity_rbac_enabled": _bool_true(before.get(TARGET_KEY)),
    }
    if str(before.get("APP_ENV") or "").lower() != "production":
        return 1, {**payload, "verdict": "FAIL", "reason": "PRODUCTION_PRESTATE_APP_ENV_INVALID"}
    if str(before.get("CAPTURE_ENGINE_VERSION") or "").upper() != "V1":
        return 1, {**payload, "verdict": "FAIL", "reason": "PRODUCTION_PRESTATE_NOT_V1"}
    if not _bool_false(before.get("CAPTURE_V2_PRODUCTION_ENABLED")):
        return 1, {**payload, "verdict": "FAIL", "reason": "PRODUCTION_PRESTATE_V2_ALREADY_ENABLED"}

    if _bool_true(before.get(TARGET_KEY)):
        return 0, {
            **payload,
            "verdict": "PASS",
            "reason": "PRODUCTION_FEISHU_RBAC_ALREADY_ENABLED",
            "poststate": {"feishu_identity_rbac_enabled": True},
        }

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = PRODUCTION_ENV.with_name(PRODUCTION_ENV.name + f".feishu-rbac-pre-{stamp}")
    mutated = False
    try:
        shutil.copy2(PRODUCTION_ENV, backup)
        os.chmod(backup, 0o600)
        _write_only_target(PRODUCTION_ENV)
        mutated = True
        payload["mutations_performed"] = True
        after = _parse_safe_env(PRODUCTION_ENV)
        if not _bool_true(after.get(TARGET_KEY)):
            raise RuntimeError("TARGET_VALUE_VERIFY_FAILED")
        # Prove no V1/V2 authority prestate key changed as part of this prerequisite.
        for key in ("APP_ENV", "CAPTURE_ENGINE_VERSION", "CAPTURE_V2_PRODUCTION_ENABLED", "REPRODUCTION_PLATFORM_MODE"):
            if before.get(key) != after.get(key):
                raise RuntimeError(f"UNEXPECTED_ENV_CHANGE:{key}")
        payload["backup_path"] = str(backup)
        payload["changed_keys"] = [TARGET_KEY]
        payload["poststate"] = {"feishu_identity_rbac_enabled": True}
        payload["verdict"] = "PASS"
        payload["reason"] = "PRODUCTION_FEISHU_RBAC_ENABLED"
        return 0, payload
    except Exception as exc:
        rollback = {"attempted": mutated, "env_restored": False}
        if mutated and backup.is_file():
            try:
                shutil.copy2(backup, PRODUCTION_ENV)
                os.chmod(PRODUCTION_ENV, 0o600)
                rollback["env_restored"] = True
            except Exception as restore_exc:
                rollback["error"] = type(restore_exc).__name__
        payload["rollback"] = rollback
        reason = "PRODUCTION_FEISHU_RBAC_ENABLE_FAILED"
        if mutated and not rollback.get("env_restored"):
            reason = "PRODUCTION_FEISHU_RBAC_ENABLE_FAILED_ROLLBACK_UNVERIFIED"
        return 1, {
            **payload,
            "verdict": "FAIL",
            "reason": reason,
            "error": type(exc).__name__,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Guarded production Feishu identity RBAC prerequisite enable")
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--authorization", type=Path, required=True)
    args = parser.parse_args(argv)
    rc, payload = run(repo_root=args.repo_root, authorization_path=args.authorization)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
