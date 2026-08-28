#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess


def main() -> int:
    if os.getenv("DATABASE_URL", "").strip():
        return 0

    ps = subprocess.check_output(
        ["docker", "ps", "--format", "{{.ID}}"], text=True
    )
    candidates: list[tuple[str, str]] = []
    for cid in ps.splitlines():
        cid = cid.strip()
        if not cid:
            continue
        data = json.loads(subprocess.check_output(["docker", "inspect", cid], text=True))[0]
        env = {}
        for item in ((data.get("Config") or {}).get("Env") or []):
            key, sep, value = str(item).partition("=")
            if sep:
                env[key] = value
        url = str(env.get("DATABASE_URL") or "").strip()
        if not url:
            continue
        # The live Gate is a Production-only mutation path. Development/acceptance
        # stacks may coexist on the controlled runner and must never participate
        # in Production DB consensus.
        if str(env.get("APP_ENV") or "").strip().lower() != "production":
            continue
        if str(env.get("REPRODUCTION_PLATFORM_MODE") or "").strip().lower() != "real":
            continue
        name = str((data.get("Name") or "")).lstrip("/")
        candidates.append((name, url))

    unique_urls = {url for _, url in candidates}
    if len(unique_urls) != 1:
        raise SystemExit(
            "SIP_ABA_PRODUCTION_DATABASE_SOURCE_NOT_UNIQUE "
            f"production_real_containers_with_db={len(candidates)} unique_urls={len(unique_urls)}"
        )

    url = next(iter(unique_urls))
    # Emit only shell syntax. Caller redirects this into the private runtime dir;
    # never print the URL to logs or uploaded evidence.
    import shlex
    print(f"DATABASE_URL={shlex.quote(url)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
