#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "release" / "source_manifest.json"

INCLUDE_DIRS = [
    "backend/app", "backend/migrations", "frontend/src", "profiles", "rules", "knowledge", "tools", "contracts", "deploy"
]
INCLUDE_FILES = [
    "Makefile", ".env.example", "backend/requirements.txt", "backend/alembic.ini",
    "backend/run_feishu_long_connection.py",
    "frontend/package.json", "frontend/package-lock.json", "frontend/tsconfig.json", "frontend/vite.config.ts",
    "frontend/Dockerfile", "frontend/nginx.conf", "backend/Dockerfile",
    "docker-compose.yml", "docker-compose.production.yml", "docker-compose.e2e.yml", "release/release_policy.yaml",
    ".github/workflows/production-deploy.yml", ".github/workflows/source-manifest-gate.yml",
    ".github/workflows/evidence-v2-production-golden-bootstrap.yml",
]
EXCLUDE_NAMES = {"__pycache__", ".pytest_cache", ".DS_Store"}
EXCLUDE_FILES = {"release/source_manifest.json"}


def files() -> list[Path]:
    out: set[Path] = set()
    for d in INCLUDE_DIRS:
        base = ROOT / d
        if not base.exists():
            continue
        for p in base.rglob("*"):
            if p.is_file() and not any(part in EXCLUDE_NAMES for part in p.parts):
                out.add(p)
    for f in INCLUDE_FILES:
        p = ROOT / f
        if p.exists():
            out.add(p)
    return sorted([p for p in out if str(p.relative_to(ROOT)).replace("\\", "/") not in EXCLUDE_FILES], key=lambda p: str(p.relative_to(ROOT)))


def build() -> dict:
    rows = []
    for path in files():
        data = path.read_bytes()
        rows.append({
            "path": str(path.relative_to(ROOT)).replace("\\", "/"),
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        })
    aggregate = hashlib.sha256("\n".join(f"{x['path']}:{x['sha256']}" for x in rows).encode()).hexdigest()
    return {"schema_version": 1, "file_count": len(rows), "aggregate_sha256": aggregate, "files": rows}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--update", action="store_true")
    args = ap.parse_args()
    current = build()
    if args.update:
        MANIFEST.parent.mkdir(parents=True, exist_ok=True)
        MANIFEST.write_text(json.dumps(current, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    errors = []
    if not MANIFEST.exists():
        errors.append("source manifest missing; run with --update")
    else:
        saved = json.loads(MANIFEST.read_text(encoding="utf-8"))
        if saved != current:
            errors.append("source manifest drift detected")
    print(json.dumps({
        "status": "PASS" if not errors else "FAIL",
        "file_count": current["file_count"],
        "aggregate_sha256": current["aggregate_sha256"],
        "errors": errors,
    }, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
