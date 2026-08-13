#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[1]


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def main() -> int:
    errors: list[str] = []
    prod = load(ROOT / "docker-compose.yml")
    e2e = load(ROOT / "docker-compose.e2e.yml")
    prod_override = load(ROOT / "docker-compose.production.yml")
    prod_services = prod.get("services") or {}
    e2e_services = e2e.get("services") or {}
    prod_override_services = prod_override.get("services") or {}
    required_prod = {
        "postgres", "redis", "minio", "backend", "collector-worker", "packet-worker",
        "pcm-worker", "media-worker", "diagnosis-worker", "reproduction-worker", "frontend",
    }
    required_e2e = {"postgres", "redis", "minio", "backend", "media-worker", "diagnosis-worker", "e2e-runner"}
    missing = sorted(required_prod - set(prod_services))
    if missing:
        errors.append(f"docker-compose.yml missing services: {missing}")
    missing = sorted(required_e2e - set(e2e_services))
    if missing:
        errors.append(f"docker-compose.e2e.yml missing services: {missing}")
    for filename, services in (("docker-compose.yml", prod_services), ("docker-compose.production.yml", prod_override_services), ("docker-compose.e2e.yml", e2e_services)):
        for name, cfg in services.items():
            if cfg.get("privileged") is True:
                errors.append(f"{filename}:{name}: privileged containers are forbidden")
            if cfg.get("network_mode") == "host":
                errors.append(f"{filename}:{name}: host networking is forbidden")
    if "healthcheck" not in prod_services.get("postgres", {}):
        errors.append("postgres healthcheck required")
    if "healthcheck" not in prod_services.get("redis", {}):
        errors.append("redis healthcheck required")
    backend_cmd = str(prod_services.get("backend", {}).get("command", ""))
    if "alembic upgrade head" not in backend_cmd:
        errors.append("backend startup must run alembic upgrade head")
    e2e_health = e2e_services.get("backend", {}).get("healthcheck", {})
    if "/health/ready" not in str(e2e_health.get("test", "")):
        errors.append("E2E backend healthcheck must use /health/ready")
    e2e_env = e2e_services.get("e2e-runner", {}).get("environment", {}) or {}
    if "SOURCE_MANIFEST_SHA256" not in e2e_env:
        errors.append("E2E runner must receive SOURCE_MANIFEST_SHA256 for exact-source runtime evidence")
    if "EXPECTED_ALEMBIC_HEAD" not in e2e_env:
        errors.append("E2E runner must receive EXPECTED_ALEMBIC_HEAD for PostgreSQL migration verification")
    if "release-runner" not in prod_override_services:
        errors.append("docker-compose.production.yml missing release-runner")
    required_secret_names={"auth_gateway_hmac","minio_access_key","minio_secret_key","credential_api_token","feishu_app_secret","feishu_verification_token"}
    missing_secrets=sorted(required_secret_names-set((prod_override.get("secrets") or {}).keys()))
    if missing_secrets:
        errors.append(f"docker-compose.production.yml missing secrets: {missing_secrets}")
    payload = {
        "status": "PASS" if not errors else "FAIL",
        "production_services": len(prod_services),
        "production_override_services": len(prod_override_services),
        "e2e_services": len(e2e_services),
        "errors": errors,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
