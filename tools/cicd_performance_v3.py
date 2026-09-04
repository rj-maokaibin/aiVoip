#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _revision() -> str:
    configured = os.environ.get("EXPECTED_SHA") or os.environ.get("BUILD_REVISION")
    if configured:
        return configured
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    except Exception:
        return "unknown"


def _load(path: Path) -> dict:
    if not path.exists():
        return {
            "schema_version": "cicd-performance-v3",
            "created_at": _now(),
            "updated_at": _now(),
            "build_revision": _revision(),
            "status": "PASS",
            "total_duration_ms": 0,
            "phases": [],
        }
    return json.loads(path.read_text(encoding="utf-8"))


def _save(path: Path, payload: dict) -> None:
    payload["updated_at"] = _now()
    payload["total_duration_ms"] = sum(int(x.get("duration_ms") or 0) for x in payload.get("phases", []))
    payload["status"] = "PASS" if payload.get("phases") and all(x.get("status") == "PASS" for x in payload["phases"]) else "FAIL"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="Append machine-readable CI/CD Performance V2.3 phase evidence")
    ap.add_argument("--out", type=Path, default=Path("validation/cicd_performance_v3.json"))
    sub = ap.add_subparsers(dest="command", required=True)

    record = sub.add_parser("record")
    record.add_argument("--phase", required=True)
    record.add_argument("--status", choices=["PASS", "FAIL"], required=True)
    record.add_argument("--duration-ms", type=int, required=True)
    record.add_argument("--meta", action="append", default=[])

    sub.add_parser("summary")
    args = ap.parse_args()
    out = args.out.resolve()
    payload = _load(out)

    if args.command == "record":
        meta: dict[str, str] = {}
        for item in args.meta:
            if "=" not in item:
                raise SystemExit(f"invalid --meta value: {item!r}; expected key=value")
            key, value = item.split("=", 1)
            meta[key] = value
        payload.setdefault("phases", []).append({
            "phase": args.phase,
            "status": args.status,
            "duration_ms": max(0, args.duration_ms),
            "meta": meta,
        })
        _save(out, payload)
        print(
            f"PERF_PHASE_V3 name={args.phase} status={args.status} "
            f"duration_ms={max(0, args.duration_ms)}"
        )
        return 0 if args.status == "PASS" else 1

    _save(out, payload)
    print(
        f"CICD_PERFORMANCE_V3_EVIDENCE={out} "
        f"total_ms={payload['total_duration_ms']} status={payload['status']}"
    )
    return 0 if payload["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
