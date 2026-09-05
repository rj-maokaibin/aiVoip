#!/usr/bin/env python3
from __future__ import annotations

import json
import shlex
import subprocess


# Production Deploy verifies every live service under this exact Compose project.
# Keep Real Gate credential discovery source-bound to the same deployment identity.
PRODUCTION_PROJECT = "aivoip"
PRODUCTION_CREDENTIAL_SERVICE = "reproduction-worker"


def main() -> int:
    ps = subprocess.check_output(["docker", "ps", "--format", "{{.ID}}"], text=True)
    production_real: list[dict[str, object]] = []
    for cid in ps.splitlines():
        cid = cid.strip()
        if not cid:
            continue
        data = json.loads(subprocess.check_output(["docker", "inspect", cid], text=True))[0]
        env: dict[str, str] = {}
        for item in ((data.get("Config") or {}).get("Env") or []):
            key, sep, value = str(item).partition("=")
            if sep:
                env[key] = value
        if str(env.get("APP_ENV") or "").strip().lower() != "production":
            continue
        if str(env.get("REPRODUCTION_PLATFORM_MODE") or "").strip().lower() != "real":
            continue
        labels = dict((data.get("Config") or {}).get("Labels") or {})
        production_real.append(
            {
                "name": str(data.get("Name") or "").lstrip("/"),
                "env": env,
                "labels": labels,
            }
        )

    db_candidates = [
        row for row in production_real if str((row["env"] or {}).get("DATABASE_URL") or "").strip()
    ]
    unique_urls = {
        str((row["env"] or {}).get("DATABASE_URL") or "").strip() for row in db_candidates
    }
    if len(unique_urls) != 1:
        raise SystemExit(
            "SIP_ABA_PRODUCTION_DATABASE_SOURCE_NOT_UNIQUE "
            f"production_real_containers_with_db={len(db_candidates)} unique_urls={len(unique_urls)}"
        )

    credential_workers = []
    for row in production_real:
        labels = row["labels"] or {}
        if str(labels.get("com.docker.compose.project") or "") != PRODUCTION_PROJECT:
            continue
        if str(labels.get("com.docker.compose.service") or "") != PRODUCTION_CREDENTIAL_SERVICE:
            continue
        credential_workers.append(row)
    if len(credential_workers) != 1:
        raise SystemExit(
            "SIP_ABA_PRODUCTION_CREDENTIAL_RUNTIME_NOT_UNIQUE "
            f"project={PRODUCTION_PROJECT} service={PRODUCTION_CREDENTIAL_SERVICE} "
            f"count={len(credential_workers)}"
        )

    worker = credential_workers[0]
    worker_env = worker["env"] or {}
    provider = str(worker_env.get("CREDENTIAL_PROVIDER") or "").strip().lower()
    if not provider:
        raise SystemExit("SIP_ABA_PRODUCTION_CREDENTIAL_PROVIDER_MISSING")

    values = {
        "DATABASE_URL": next(iter(unique_urls)),
        "SIP_ABA_PRODUCTION_CREDENTIAL_CONTAINER": str(worker["name"]),
        "SIP_ABA_PRODUCTION_CREDENTIAL_PROVIDER": provider,
    }
    # Emit only shell assignments. DATABASE_URL is secret-bearing; caller redirects
    # this into the already-private 0700 runtime directory and never uploads it.
    for key, value in values.items():
        print(f"{key}={shlex.quote(value)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
