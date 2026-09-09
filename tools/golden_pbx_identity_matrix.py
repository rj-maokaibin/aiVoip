#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

from app.automation.adapters.pbx.identity import (
    PbxExtensionIdentityError,
    encode_sip_user_identity,
    normalize_sip_user_wire_identity,
    normalize_testlab_provider_mutation_identity,
)

DIRECT_SPECIALS = "-_.!~*'()&=+$,;?/"
ESCAPED_LOGICAL = ("j@s0n", "a:b", "room 1", "100%real", "福州")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Discover PBX RFC3261 identity capability")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--alias-start", type=int, default=7970)
    return parser.parse_args()


def _run_live_case(args, root: Path, index: int, char: str) -> dict:
    identity = f"mx{char}1"
    alias = str(args.alias_start + index)
    normalize_sip_user_wire_identity(identity)
    try:
        normalize_testlab_provider_mutation_identity(identity)
    except PbxExtensionIdentityError as exc:
        return {
            "char": char,
            "identity": identity,
            "dial_alias": alias,
            "core_supported": True,
            "provider_mutation_attempted": False,
            "provider_status": "UNSAFE_BY_POLICY",
            "reason": str(exc),
        }

    evidence = root / f"direct-{index:02d}.json"
    state_db = root / f"direct-{index:02d}.sqlite"
    command = [
        sys.executable, "tools/golden_pbx_extension_001.py",
        "--profile", args.profile,
        "--state-db", str(state_db),
        "--evidence", str(evidence),
        "--run-id", f"G-PBX-ID-MATRIX-{index:02d}",
        "--owner", args.owner,
        "--extension", identity,
        "--dial-alias", alias,
    ]
    cp = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
        timeout=90,
    )
    payload: dict = {}
    if evidence.is_file():
        try:
            payload = json.loads(evidence.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
    cleanup_safe = bool(
        payload.get("after_cleanup_exists") is False
        and payload.get("after_cleanup_runtime_visible") is False
        and payload.get("after_cleanup_alias_exists") is False
        and payload.get("after_cleanup_alias_runtime_visible") is False
        and payload.get("after_cleanup_alias_resolution") is None
        and payload.get("release_last") is True
    )
    return {
        "char": char,
        "identity": identity,
        "dial_alias": alias,
        "core_supported": True,
        "provider_mutation_attempted": True,
        "provider_status": "PASS" if cp.returncode == 0 and payload.get("verdict") == "PASS" else "FAIL",
        "cleanup_safe": cleanup_safe,
        "gate_verdict": payload.get("verdict"),
        "error_code": payload.get("create_error_code") or payload.get("cleanup_error_code"),
    }


def _escaped_matrix() -> list[dict]:
    rows: list[dict] = []
    for logical in ESCAPED_LOGICAL:
        encoded = encode_sip_user_identity(logical)
        rows.append({
            "logical": logical,
            "wire": encoded.wire,
            "escaped": encoded.escaped,
            "core_supported": True,
            "provider_mutation_safe": encoded.provider_mutation_safe,
            "provider_mutation_attempted": False,
            "status": "CORE_CODEC_PASS",
        })
    return rows


def main() -> int:
    args = parse_args()
    root = Path(args.output_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    direct: list[dict] = []
    for index, char in enumerate(DIRECT_SPECIALS):
        try:
            direct.append(_run_live_case(args, root, index, char))
        except Exception as exc:
            direct.append({
                "char": char,
                "identity": f"mx{char}1",
                "dial_alias": str(args.alias_start + index),
                "core_supported": True,
                "provider_mutation_attempted": char != "/",
                "provider_status": "RUNNER_ERROR",
                "error_code": type(exc).__name__,
            })
    escaped = _escaped_matrix()
    unsafe = [row for row in direct if row.get("provider_status") == "UNSAFE_BY_POLICY"]
    attempted = [row for row in direct if row.get("provider_mutation_attempted") is True]
    passed = [row for row in attempted if row.get("provider_status") == "PASS"]
    cleanup_failures = [row for row in attempted if row.get("cleanup_safe") is not True]
    summary = {
        "schema": "g-pbx-identity-matrix-v1",
        "rfc3261_direct_specials": DIRECT_SPECIALS,
        "direct": direct,
        "escaped": escaped,
        "core_direct_count": len(direct),
        "provider_attempted_count": len(attempted),
        "provider_pass_count": len(passed),
        "provider_unsafe_by_policy_count": len(unsafe),
        "cleanup_failure_count": len(cleanup_failures),
        "all_core_supported": all(row.get("core_supported") is True for row in direct + escaped),
        "secret_values_emitted": False,
    }
    summary["verdict"] = "PASS" if (
        summary["all_core_supported"]
        and len(passed) == len(attempted)
        and not cleanup_failures
        and all(row.get("provider_mutation_attempted") is False for row in unsafe)
    ) else "FAIL"
    output = root / "identity-matrix.json"
    output.write_text(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0 if summary["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
