#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    errors: list[str] = []
    checks: dict[str, bool] = {}
    required = [
        ROOT / "deploy/voip-ai",
        ROOT / "deploy/deployment_preflight.py",
        ROOT / "deploy/production_runtime_verify.py",
        ROOT / "docker-compose.production.yml",
        ROOT / "frontend/nginx.conf",
        ROOT / "deploy/production.env.example",
    ]
    missing = [str(x.relative_to(ROOT)) for x in required if not x.exists()]
    checks["required_files"] = not missing
    if missing:
        errors.append("missing deployment files: " + ", ".join(missing))

    cli = (ROOT / "deploy/voip-ai").read_text(encoding="utf-8") if (ROOT / "deploy/voip-ai").exists() else ""
    for token in ["preflight", "prepare-host", "deploy", "verify", "release", "backup-db", "--env-file", "alembic upgrade head", "production_runtime_result.json"]:
        if token not in cli:
            errors.append(f"deploy/voip-ai missing contract token: {token}")
    checks["safe_no_volume_destroy"] = "down -v" not in cli and "docker volume rm" not in cli and "rm -rf /data" not in cli
    if not checks["safe_no_volume_destroy"]:
        errors.append("production deployment CLI contains a destructive data/volume command")
    checks["cli_executable"] = os.access(ROOT / "deploy/voip-ai", os.X_OK)
    if not checks["cli_executable"]:
        errors.append("deploy/voip-ai must be executable")

    prod = yaml.safe_load((ROOT / "docker-compose.production.yml").read_text(encoding="utf-8")) or {}
    services = prod.get("services") or {}
    checks["release_runner_service"] = "release-runner" in services
    if not checks["release_runner_service"]:
        errors.append("production compose override missing release-runner")
    secret_names = set((prod.get("secrets") or {}).keys())
    required_secrets = {"auth_gateway_hmac", "minio_access_key", "minio_secret_key", "credential_api_token", "feishu_app_secret", "feishu_verification_token"}
    checks["docker_secret_mounts"] = required_secrets <= secret_names
    if not checks["docker_secret_mounts"]:
        errors.append(f"production compose missing secrets: {sorted(required_secrets-secret_names)}")

    nginx = (ROOT / "frontend/nginx.conf").read_text(encoding="utf-8")
    checks["same_origin_api_proxy"] = "location /api/" in nginx and "proxy_pass http://backend:8000" in nginx and "proxy_buffering off" in nginx
    if not checks["same_origin_api_proxy"]:
        errors.append("frontend nginx same-origin API/SSE proxy contract is incomplete")
    api = (ROOT / "frontend/src/api.ts").read_text(encoding="utf-8")
    checks["frontend_relative_api_default"] = "'/api/v1'" in api and "localhost:8000" not in api
    if not checks["frontend_relative_api_default"]:
        errors.append("frontend must default to same-origin /api/v1 in production")

    env = (ROOT / "deploy/production.env.example").read_text(encoding="utf-8")
    host_secret_keys = [
        "AUTH_GATEWAY_HMAC_SECRET_HOST_FILE", "MINIO_ACCESS_KEY_SECRET_HOST_FILE",
        "MINIO_SECRET_KEY_SECRET_HOST_FILE", "CREDENTIAL_API_TOKEN_SECRET_HOST_FILE",
        "FEISHU_APP_SECRET_HOST_FILE", "FEISHU_VERIFICATION_TOKEN_HOST_FILE",
    ]
    checks["host_secret_contract"] = all(k in env for k in host_secret_keys)
    if not checks["host_secret_contract"]:
        errors.append("production.env.example is missing host Docker-secret file mappings")

    payload = {"status": "PASS" if not errors else "FAIL", "checks": checks, "errors": errors}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
