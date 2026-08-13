#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def check_contains(path: Path, needles: list[str], errors: list[str]) -> None:
    text = path.read_text(encoding="utf-8")
    for needle in needles:
        if needle not in text:
            errors.append(f"{path.relative_to(ROOT)} missing required contract token: {needle}")


def main() -> int:
    errors: list[str] = []
    checks: dict[str, bool] = {}

    required = [
        ROOT / "backend/app/auth/providers.py",
        ROOT / "backend/app/integrations/secrets.py",
        ROOT / "backend/app/integrations/feishu/transport.py",
        ROOT / "backend/app/integrations/feishu/service.py",
        ROOT / "backend/app/api/v1/feishu_callback.py",
        ROOT / "backend/app/production_config.py",
        ROOT / "backend/migrations/versions/0011_phase_f2_production_hardening.py",
        ROOT / "deploy/production.env.example",
        ROOT / "deploy/SECRETS.md",
    ]
    missing = [str(x.relative_to(ROOT)) for x in required if not x.exists()]
    checks["required_f2_files"] = not missing
    if missing:
        errors.append("missing F2 files: " + ", ".join(missing))

    check_contains(ROOT / "backend/app/auth/providers.py", ["gateway_hmac", "hmac.compare_digest", "AUTH_SIGNATURE_EXPIRED"], errors)
    check_contains(ROOT / "backend/app/integrations/feishu/transport.py", ["tenant_access_token/internal", "/im/v1/messages", "PATCH", "X-Lark" if False else "FEISHU_CALLBACK_SIGNATURE_INVALID"], errors)
    check_contains(ROOT / "frontend/Dockerfile", ["package-lock.json", "npm ci"], errors)
    dockerfile = (ROOT / "frontend/Dockerfile").read_text(encoding="utf-8")
    checks["frontend_reproducible_docker_contract"] = "npm install" not in dockerfile
    if not checks["frontend_reproducible_docker_contract"]:
        errors.append("frontend Dockerfile still uses npm install")

    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    checks["compose_no_default_postgres_password"] = "POSTGRES_PASSWORD: voip" not in compose
    checks["compose_no_default_minio_root"] = "MINIO_ROOT_PASSWORD: voipminiosecret" not in compose
    if not checks["compose_no_default_postgres_password"]:
        errors.append("production compose contains default PostgreSQL password")
    if not checks["compose_no_default_minio_root"]:
        errors.append("production compose contains default MinIO password")

    env_text = (ROOT / ".env.example").read_text(encoding="utf-8")
    checks["secret_file_contract_exposed"] = all(x in env_text for x in ["AUTH_GATEWAY_HMAC_SECRET_FILE", "MINIO_SECRET_KEY_FILE", "CREDENTIAL_API_TOKEN_FILE", "FEISHU_APP_SECRET_FILE"])
    if not checks["secret_file_contract_exposed"]:
        errors.append(".env.example does not expose required secret-file references")

    checks["auth_implementation"] = not any("auth/providers.py" in x for x in errors)
    checks["feishu_implementation"] = not any("feishu/transport.py" in x for x in errors)
    payload = {"status": "PASS" if not errors else "FAIL", "checks": checks, "errors": errors}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
