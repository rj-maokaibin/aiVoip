#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.golden.offline_analysis_e2e import (
    build_offline_analysis_bundle,
    sha256_file,
    validation_payload,
)


DEFAULT_MANIFEST = ROOT / "golden_cases" / "OFFLINE_ANALYSIS_20260814_001" / "manifest.yaml"


def _resolve_fixture(args, manifest: dict) -> Path | None:
    if args.pcap:
        return Path(args.pcap)
    env_name = str((manifest.get("source") or {}).get("fixture_env") or "").strip()
    if env_name and os.getenv(env_name):
        return Path(os.environ[env_name])
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay an imported-PCAP Offline Analysis Golden E2E case")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--pcap", type=Path)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--tshark", default=os.getenv("TSHARK_BINARY", "tshark"))
    parser.add_argument("--artifacts", type=Path, default=Path("offline-golden-artifacts"))
    parser.add_argument("--result", type=Path, default=Path("offline-golden-result.json"))
    parser.add_argument("--require-fixture", action="store_true", help="Treat a missing external PCAP fixture as a blocking failure")
    args = parser.parse_args()

    manifest = yaml.safe_load(args.manifest.read_text(encoding="utf-8"))
    fixture = _resolve_fixture(args, manifest)
    if fixture is None or not fixture.exists():
        payload = {
            "schema_version": "offline-analysis-golden-result-v1",
            "golden_case": manifest.get("id"),
            "status": "UNAVAILABLE",
            "passed": False,
            "reason": "EXTERNAL_FIXTURE_NOT_CONFIGURED",
            "fixture_env": (manifest.get("source") or {}).get("fixture_env"),
            "expected_filename": (manifest.get("source") or {}).get("filename"),
            "classification": manifest.get("classification"),
        }
        args.result.parent.mkdir(parents=True, exist_ok=True)
        args.result.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 2 if args.require_fixture else 3

    expected_sha = str((manifest.get("source") or {}).get("sha256") or "")
    actual_sha = sha256_file(fixture)
    if expected_sha and actual_sha != expected_sha:
        payload = {
            "schema_version": "offline-analysis-golden-result-v1",
            "golden_case": manifest.get("id"),
            "status": "FAILED",
            "passed": False,
            "reason": "SOURCE_SHA256_MISMATCH",
            "actual_sha256": actual_sha,
            "expected_sha256": expected_sha,
        }
        args.result.parent.mkdir(parents=True, exist_ok=True)
        args.result.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 2

    profile_id = str((manifest.get("source") or {}).get("pcm_profile") or "ruijie_aim_diag_v1")
    profile_path = args.profile or ROOT / "profiles" / "pcm" / f"{profile_id}.yaml"
    bundle = build_offline_analysis_bundle(
        pcap_path=fixture,
        pcm_profile_path=profile_path,
        output_dir=args.artifacts,
        tshark_binary=args.tshark,
    )
    payload = validation_payload(bundle, manifest)
    payload["status"] = "PASS" if payload["passed"] else "FAIL"
    payload["fixture_path"] = str(fixture)
    payload["manifest_path"] = str(args.manifest)
    args.result.parent.mkdir(parents=True, exist_ok=True)
    args.result.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "golden_case": payload["golden_case"],
        "status": payload["status"],
        "checks_passed": payload["checks_passed"],
        "checks_total": payload["checks_total"],
        "result": str(args.result),
    }, ensure_ascii=False, indent=2))
    return 0 if payload["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
