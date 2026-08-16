#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import stat
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
PLACEHOLDER = re.compile(r"<[^>]+>")
DEFAULT_SECRET_VALUES = {"", "change-me", "minioadmin", "voipminio", "voipminiosecret", "password", "secret"}


@dataclass
class Check:
    key: str
    status: str
    category: str
    detail: str
    blocks_deploy: bool = False
    blocks_release: bool = False


def parse_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def secure_file(path: Path) -> tuple[bool, str]:
    try:
        if not path.exists():
            return False, "missing"
        if not path.is_file():
            return False, "not a regular file"
        metadata = path.stat()
        if metadata.st_size <= 0:
            return False, "empty"
        mode = stat.S_IMODE(metadata.st_mode)
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            return False, f"mode {mode:04o} is group/world accessible"
        return True, f"mode {mode:04o}, {metadata.st_size} bytes"
    except OSError as exc:
        # A preflight check must fail closed and keep producing its machine-
        # readable report even when the invoking account cannot traverse a
        # production secret directory.
        return False, f"unreadable ({type(exc).__name__})"


def bool_value(v: str) -> bool:
    return str(v).strip().lower() in {"1", "true", "yes", "on"}


def main() -> int:
    ap = argparse.ArgumentParser(description="VOIP AI production deployment preflight")
    ap.add_argument("--env-file", type=Path, required=True)
    ap.add_argument("--mode", choices=["deploy", "release"], default="deploy")
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()

    checks: list[Check] = []
    env_file = args.env_file.expanduser().resolve()
    if not env_file.exists():
        payload = {"schema_version": 1, "status": "BLOCKED", "deployment_status": "BLOCKED", "release_status": "BLOCKED", "checks": [asdict(Check("ENV_FILE", "BLOCKED", "CONFIG", f"missing env file: {env_file}", True, True))]}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 2

    ok, detail = secure_file(env_file)
    checks.append(Check("ENV_FILE_PERMISSIONS", "PASS" if ok else "BLOCKED", "SECURITY", detail, not ok, not ok))
    values = parse_dotenv(env_file)

    placeholder_keys = sorted(k for k, v in values.items() if PLACEHOLDER.search(v))
    checks.append(Check(
        "NO_PLACEHOLDERS", "PASS" if not placeholder_keys else "BLOCKED", "CONFIG",
        "all placeholders replaced" if not placeholder_keys else "unresolved placeholders: " + ", ".join(placeholder_keys),
        bool(placeholder_keys), bool(placeholder_keys),
    ))

    required = [
        "APP_ENV", "BUILD_REVISION", "POSTGRES_PASSWORD", "DATABASE_URL", "REDIS_URL",
        "MINIO_ROOT_USER", "MINIO_ROOT_PASSWORD", "MINIO_ENDPOINT", "MINIO_BUCKET",
        "CREDENTIAL_PROVIDER", "CREDENTIAL_API_URL", "PRODUCTION_AUTH_PROVIDER",
        "CORS_ALLOW_ORIGINS", "REPRODUCTION_STORAGE_MODE", "REPRODUCTION_PLATFORM_MODE",
    ]
    missing = [k for k in required if not values.get(k, "").strip()]
    checks.append(Check("REQUIRED_ENV_KEYS", "PASS" if not missing else "BLOCKED", "CONFIG", "required keys present" if not missing else "missing: " + ", ".join(missing), bool(missing), bool(missing)))

    prod_env = values.get("APP_ENV", "").lower() == "production"
    checks.append(Check("APP_ENV_PRODUCTION", "PASS" if prod_env else "BLOCKED", "CONFIG", f"APP_ENV={values.get('APP_ENV','')}", not prod_env, not prod_env))

    rev = values.get("BUILD_REVISION", "").strip()
    rev_ok = bool(rev and rev.lower() not in {"dev", "local", "unknown"} and not PLACEHOLDER.search(rev))
    checks.append(Check("BUILD_REVISION_PINNED", "PASS" if rev_ok else "BLOCKED", "BUILD", rev if rev else "not configured", not rev_ok, not rev_ok))

    anon = bool_value(values.get("AUTH_ALLOW_ANONYMOUS_DEV", "true"))
    auth_ok = values.get("PRODUCTION_AUTH_PROVIDER", "").lower() == "gateway_hmac" and not anon
    checks.append(Check("PRODUCTION_AUTH_MODE", "PASS" if auth_ok else "BLOCKED", "SECURITY", "gateway_hmac + anonymous disabled" if auth_ok else "production auth must be gateway_hmac and anonymous dev auth disabled", not auth_ok, not auth_ok))

    origins = [x.strip() for x in values.get("CORS_ALLOW_ORIGINS", "").split(",") if x.strip()]
    cors_ok = bool(origins) and "*" not in origins and all(urlparse(x).scheme in {"http", "https"} and urlparse(x).netloc for x in origins)
    checks.append(Check("PRODUCTION_CORS", "PASS" if cors_ok else "BLOCKED", "SECURITY", ",".join(origins) if origins else "not configured", not cors_ok, not cors_ok))

    pg = values.get("POSTGRES_PASSWORD", "").strip()
    minio_user = values.get("MINIO_ROOT_USER", "").strip()
    minio_pw = values.get("MINIO_ROOT_PASSWORD", "").strip()
    boot_creds_ok = all(x.lower() not in DEFAULT_SECRET_VALUES and len(x) >= 12 for x in [pg, minio_user, minio_pw])
    checks.append(Check("BOOTSTRAP_CREDENTIALS", "PASS" if boot_creds_ok else "BLOCKED", "SECURITY", "non-default bootstrap credentials configured" if boot_creds_ok else "PostgreSQL/MinIO bootstrap credentials must be non-default and >=12 chars", not boot_creds_ok, not boot_creds_ok))

    db_url = values.get("DATABASE_URL", "")
    db_ok = bool(db_url.startswith("postgresql+psycopg://") and pg and pg in db_url and not PLACEHOLDER.search(db_url))
    checks.append(Check("DATABASE_URL_MATCH", "PASS" if db_ok else "BLOCKED", "CONFIG", "DATABASE_URL contains configured PostgreSQL credential" if db_ok else "DATABASE_URL must be a resolved psycopg URL matching POSTGRES_PASSWORD", not db_ok, not db_ok))

    storage_ok = values.get("REPRODUCTION_STORAGE_MODE", "").lower() == "minio"
    checks.append(Check("PRODUCTION_STORAGE_MODE", "PASS" if storage_ok else "BLOCKED", "STORAGE", values.get("REPRODUCTION_STORAGE_MODE", ""), not storage_ok, not storage_ok))

    credential_ok = values.get("CREDENTIAL_PROVIDER", "").lower() == "api" and bool(values.get("CREDENTIAL_API_URL", "").strip())
    checks.append(Check("CREDENTIAL_PROVIDER", "PASS" if credential_ok else "BLOCKED", "SECURITY", "API provider configured" if credential_ok else "CREDENTIAL_PROVIDER=api and CREDENTIAL_API_URL are required", not credential_ok, not credential_ok))

    secret_vars = {
        "AUTH_GATEWAY_HMAC_SECRET_HOST_FILE": "auth gateway HMAC",
        "MINIO_ACCESS_KEY_SECRET_HOST_FILE": "MinIO access key",
        "MINIO_SECRET_KEY_SECRET_HOST_FILE": "MinIO secret key",
        "CREDENTIAL_API_TOKEN_SECRET_HOST_FILE": "credential API token",
        "FEISHU_APP_SECRET_HOST_FILE": "Feishu app secret",
        "FEISHU_VERIFICATION_TOKEN_HOST_FILE": "Feishu verification token",
    }
    for key, label in secret_vars.items():
        raw = values.get(key, "").strip()
        path = Path(raw).expanduser() if raw else Path("/__missing__")
        valid, why = secure_file(path)
        checks.append(Check(f"SECRET_{key}", "PASS" if valid else "BLOCKED", "SECRET", f"{label}: {path} ({why})", not valid, not valid))

    feishu_ok = bool_value(values.get("FEISHU_LIVE_ENABLED", "false")) and bool(values.get("FEISHU_APP_ID", "").strip()) and bool(values.get("FEISHU_DEFAULT_RECEIVE_ID", "").strip())
    checks.append(Check("FEISHU_LIVE_CONFIG", "PASS" if feishu_ok else "BLOCKED", "INTEGRATION", "live target configured" if feishu_ok else "FEISHU_LIVE_ENABLED, FEISHU_APP_ID and FEISHU_DEFAULT_RECEIVE_ID are required", not feishu_ok, not feishu_ok))

    platform_mode = values.get("REPRODUCTION_PLATFORM_MODE", "mock").lower()
    platform_ready = platform_mode not in {"", "mock", "pending"}
    checks.append(Check(
        "EC02_REAL_PLATFORM", "PASS" if platform_ready else "BLOCKED", "PLATFORM",
        f"REPRODUCTION_PLATFORM_MODE={platform_mode}; EC-02 remains pending" if not platform_ready else f"real platform mode={platform_mode}",
        False, not platform_ready,
    ))

    lockfile = ROOT / "frontend" / "package-lock.json"
    checks.append(Check("FRONTEND_LOCKFILE", "PASS" if lockfile.exists() else "BLOCKED", "BUILD", "source-controlled package-lock.json present" if lockfile.exists() else "frontend/package-lock.json missing; reproducible frontend image cannot be built", True if not lockfile.exists() else False, True if not lockfile.exists() else False))

    source_manifest = ROOT / "release" / "source_manifest.json"
    checks.append(Check("SOURCE_MANIFEST", "PASS" if source_manifest.exists() else "BLOCKED", "BUILD", str(source_manifest), not source_manifest.exists(), not source_manifest.exists()))

    deploy_blockers = [x for x in checks if x.blocks_deploy and x.status != "PASS"]
    release_blockers = [x for x in checks if x.blocks_release and x.status != "PASS"]
    deployment_status = "PASS" if not deploy_blockers else "BLOCKED"
    release_status = "PASS" if not release_blockers else "BLOCKED"
    payload = {
        "schema_version": 1,
        "status": release_status if args.mode == "release" else deployment_status,
        "mode": args.mode,
        "deployment_status": deployment_status,
        "release_status": release_status,
        "deploy_blocking_keys": [x.key for x in deploy_blockers],
        "release_blocking_keys": [x.key for x in release_blockers],
        "checks": [asdict(x) for x in checks],
    }
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
