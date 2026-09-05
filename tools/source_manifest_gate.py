#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "release" / "source_manifest.json"
RUNTIME_EXPECTED_MANIFEST = ROOT / "validation" / "source_manifest_expected.json"

INCLUDE_DIRS = [
    "backend/app", "backend/migrations", "frontend/src", "profiles", "rules", "knowledge", "tools", "contracts", "deploy"
]
INCLUDE_FILES = [
    "Makefile", ".env.example", "backend/requirements.txt", "backend/alembic.ini",
    "backend/run_feishu_long_connection.py",
    "frontend/package.json", "frontend/package-lock.json", "frontend/tsconfig.json", "frontend/vite.config.ts",
    "frontend/index.html", "frontend/evidence-report.html",
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
    return sorted(
        [p for p in out if str(p.relative_to(ROOT)).replace("\\", "/") not in EXCLUDE_FILES],
        key=lambda p: str(p.relative_to(ROOT)),
    )


def build() -> dict:
    rows = []
    for path in files():
        data = path.read_bytes()
        rows.append({
            "path": str(path.relative_to(ROOT)).replace("\\", "/"),
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        })
    aggregate = hashlib.sha256(
        "\n".join(f"{x['path']}:{x['sha256']}" for x in rows).encode()
    ).hexdigest()
    return {
        "schema_version": 1,
        "file_count": len(rows),
        "aggregate_sha256": aggregate,
        "files": rows,
    }


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _default_expected() -> Path:
    configured = os.environ.get("VOIP_SOURCE_MANIFEST_EXPECTED", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    if RUNTIME_EXPECTED_MANIFEST.exists():
        return RUNTIME_EXPECTED_MANIFEST
    return MANIFEST


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Generate or verify the deterministic VOIP source manifest without mutating source identity."
    )
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--write", type=Path, help="write the derived manifest to PATH and exit")
    mode.add_argument("--expected", type=Path, help="verify current source against an external expected manifest")
    mode.add_argument(
        "--update",
        action="store_true",
        help="legacy compatibility only: update release/source_manifest.json",
    )
    args = ap.parse_args()

    current = build()
    if args.write:
        out = args.write.expanduser().resolve()
        _write(out, current)
        print(json.dumps({
            "status": "PASS",
            "mode": "WRITE_DERIVED",
            "manifest_path": str(out),
            "file_count": current["file_count"],
            "aggregate_sha256": current["aggregate_sha256"],
            "errors": [],
        }, ensure_ascii=False, indent=2))
        return 0

    if args.update:
        _write(MANIFEST, current)
        expected = MANIFEST
        mode_name = "LEGACY_UPDATE"
    elif args.expected:
        expected = args.expected.expanduser().resolve()
        mode_name = "EXTERNAL_EXPECTED"
    else:
        expected = _default_expected()
        mode_name = "RUNTIME_EXPECTED" if expected == RUNTIME_EXPECTED_MANIFEST else "LEGACY_EXPECTED"

    errors: list[str] = []
    saved: dict | None = None
    if not expected.exists():
        errors.append(f"source manifest missing: {expected}")
    else:
        try:
            saved = json.loads(expected.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"source manifest unreadable: {expected}: {exc}")
        if saved is not None and saved != current:
            errors.append("source manifest drift detected")

    print(json.dumps({
        "status": "PASS" if not errors else "FAIL",
        "mode": mode_name,
        "manifest_path": str(expected),
        "file_count": current["file_count"],
        "aggregate_sha256": current["aggregate_sha256"],
        "errors": errors,
    }, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
