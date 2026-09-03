#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

BASE_IMAGES = (
    "python:3.12-slim", "node:22-alpine", "nginx:1.27-alpine",
    "postgres:16", "redis:7-alpine",
    "minio/minio:RELEASE.2025-04-22T22-12-26Z",
)
_NETWORK_FAILURE = re.compile(
    r"no route to host|network is unreachable|connection (?:refused|reset)|"
    r"i/o timeout|tls handshake timeout|temporary failure in name resolution|"
    r"server misbehaving|dial tcp .*timeout|context deadline exceeded|"
    r"registry probe timeout",
    re.IGNORECASE,
)
_REGISTRY_METADATA = re.compile(
    r"failed to resolve (?:source metadata|reference)|load metadata for|"
    r"failed to do request: (?:Head|Get) .*/v2/|/manifests/|"
    r"registry probe",
    re.IGNORECASE,
)


def registry_network_failure(log_text: str) -> bool:
    """Require both registry context and a network transport error."""
    return bool(_NETWORK_FAILURE.search(log_text) and _REGISTRY_METADATA.search(log_text))


def inspect_images(
    images: Sequence[str],
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> tuple[bool, list[dict]]:
    rows: list[dict] = []
    for image in images:
        cp = run(
            ["docker", "image", "inspect", image, "--format", "{{.Id}}"],
            text=True,
            capture_output=True,
        )
        present = cp.returncode == 0 and bool(cp.stdout.strip())
        rows.append({
            "image": image,
            "present": present,
            "image_id": cp.stdout.strip() if present else None,
            "error": None if present else (cp.stderr.strip() or "not found"),
        })
    return all(row["present"] for row in rows), rows


def build_audit(log_text: str, *, online_exit_code: int, images: Sequence[str], run=subprocess.run) -> dict:
    registry_failure = registry_network_failure(log_text)
    complete, inventory = inspect_images(images, run=run)
    allowed = registry_failure and complete
    if allowed:
        reason = "REGISTRY_NETWORK_FAILURE_AND_LOCAL_IMAGES_COMPLETE"
    elif not registry_failure:
        reason = "ONLINE_BUILD_FAILURE_NOT_REGISTRY_NETWORK"
    else:
        reason = "REQUIRED_LOCAL_IMAGE_MISSING"
    evidence = [
        line.strip()[:1000]
        for line in log_text.splitlines()
        if _NETWORK_FAILURE.search(line) or _REGISTRY_METADATA.search(line)
    ][:20]
    return {
        "schema_version": "production-offline-build-fallback-v2",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "ALLOWED" if allowed else "BLOCKED",
        "reason": reason,
        "online_pull_preferred": True,
        "online_build_exit_code": online_exit_code,
        "registry_network_failure": registry_failure,
        "local_image_inventory_complete": complete,
        "required_images": inventory,
        "network_evidence": evidence,
        "fallback": "compose build --pull=false" if allowed else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Fail-closed Docker offline build fallback guard")
    parser.add_argument("--build-log", type=Path, required=True)
    parser.add_argument("--online-exit-code", type=int, required=True)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--image", action="append", dest="images")
    args = parser.parse_args()
    images = tuple(args.images or BASE_IMAGES)
    log_text = args.build_log.read_text(encoding="utf-8", errors="replace")
    payload = build_audit(log_text, online_exit_code=args.online_exit_code, images=images)
    args.audit.parent.mkdir(parents=True, exist_ok=True)
    args.audit.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["status"] == "ALLOWED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
