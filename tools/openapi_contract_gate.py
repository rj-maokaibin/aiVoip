#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from offline_import_bootstrap import install as _install_offline_import_stubs  # noqa: E402
_install_offline_import_stubs()
from app.main import app  # noqa: E402

SNAPSHOT = ROOT / "contracts" / "openapi_v1.json"
SHA_FILE = ROOT / "contracts" / "openapi_v1.sha256"


def canonical(spec: dict) -> bytes:
    return (json.dumps(spec, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def validate(spec: dict) -> list[str]:
    errors: list[str] = []
    if not str(spec.get("openapi", "")).startswith("3."):
        errors.append("OpenAPI 3.x is required")
    if spec.get("info", {}).get("version") != app.version:
        errors.append("OpenAPI info.version must equal app.version")
    operation_ids: dict[str, str] = {}
    for path, item in sorted(spec.get("paths", {}).items()):
        if not (path.startswith("/api/v1/") or path.startswith("/health")):
            errors.append(f"unexpected public path outside /api/v1 or /health: {path}")
        for method, operation in item.items():
            if method.lower() not in {"get", "post", "put", "patch", "delete", "options", "head"}:
                continue
            opid = operation.get("operationId")
            if not opid:
                errors.append(f"missing operationId: {method.upper()} {path}")
                continue
            if opid in operation_ids:
                errors.append(f"duplicate operationId {opid}: {operation_ids[opid]} and {method.upper()} {path}")
            operation_ids[opid] = f"{method.upper()} {path}"
            responses = operation.get("responses") or {}
            if not responses:
                errors.append(f"missing responses: {method.upper()} {path}")
    return errors


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--update", action="store_true", help="write the frozen OpenAPI snapshot")
    args = ap.parse_args()
    spec = app.openapi()
    errors = validate(spec)
    data = canonical(spec)
    digest = hashlib.sha256(data).hexdigest()
    if args.update and not errors:
        SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT.write_bytes(data)
        SHA_FILE.write_text(digest + "  openapi_v1.json\n", encoding="utf-8")
    if not SNAPSHOT.exists():
        errors.append("OpenAPI snapshot missing; run with --update")
    elif SNAPSHOT.read_bytes() != data:
        errors.append("OpenAPI drift detected; review contract and run --update only for approved changes")
    if SHA_FILE.exists() and SNAPSHOT.exists():
        snap_digest = hashlib.sha256(SNAPSHOT.read_bytes()).hexdigest()
        declared = SHA_FILE.read_text(encoding="utf-8").split()[0]
        if snap_digest != declared:
            errors.append("openapi_v1.sha256 does not match snapshot")
    payload = {
        "status": "PASS" if not errors else "FAIL",
        "app_version": app.version,
        "path_count": len(spec.get("paths", {})),
        "operation_count": sum(1 for x in spec.get("paths", {}).values() for m in x if m.lower() in {"get","post","put","patch","delete"}),
        "sha256": digest,
        "errors": errors,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
