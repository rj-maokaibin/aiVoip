#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = Path(os.environ.get("VOIP_ACCEPTANCE_ROOT", "/opt/voip-acceptance"))
CONTRACT_INPUTS = [
    ".github/workflows/preliminary-evidence-v1.yml",
    "tools/voip_ai_release_gate.sh",
    "tools/preliminary_evidence_v1_gate.sh",
    "tools/offline_analysis_golden_replay.py",
    "tools/human_evidence_real_golden_gate.py",
    "deploy/acceptance_v2/contract.json",
    "golden_registry/real_offline_001/manifest.json",
    "backend/requirements.txt",
]


def contract_fingerprint() -> str:
    h = hashlib.sha256()
    for rel in sorted(CONTRACT_INPUTS):
        path = REPO_ROOT / rel
        h.update(rel.encode())
        h.update(b"\0")
        h.update(path.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def evidence_path(root: Path, commit: str, fingerprint: str) -> Path:
    return root / "state" / "software-evidence" / f"{commit}-{fingerprint[:16]}.json"


def required_pass(payload: dict) -> bool:
    checks = payload.get("checks") or {}
    return all(checks.get(name) == "PASS" for name in ("frozen", "software_release", "golden_142", "human_golden"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["check", "record", "key"])
    parser.add_argument("--root", default=str(DEFAULT_ROOT))
    parser.add_argument("--commit", required=True)
    parser.add_argument("--golden-sha", required=True)
    parser.add_argument("--runtime-id", default="host-prepared-v2")
    args = parser.parse_args()
    root = Path(args.root)
    fp = contract_fingerprint()
    path = evidence_path(root, args.commit, fp)
    if args.command == "key":
        print(json.dumps({"commit": args.commit, "contract_fingerprint": fp, "path": str(path)}))
        return 0
    if args.command == "check":
        if not path.is_file():
            print(json.dumps({"reusable": False, "reason": "NO_EXACT_SHA_EVIDENCE", "path": str(path)}))
            return 1
        data = json.loads(path.read_text(encoding="utf-8"))
        reusable = (
            data.get("commit") == args.commit
            and data.get("contract_fingerprint") == fp
            and data.get("golden_sha256") == args.golden_sha
            and data.get("runtime_id") == args.runtime_id
            and required_pass(data)
        )
        print(json.dumps({"reusable": reusable, "reason": "EXACT_SHA_PASS" if reusable else "EVIDENCE_CONTRACT_MISMATCH", "path": str(path)}))
        return 0 if reusable else 1
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "contract": "voip-software-evidence-v2",
        "commit": args.commit,
        "contract_fingerprint": fp,
        "golden_sha256": args.golden_sha,
        "runtime_id": args.runtime_id,
        "checks": {
            "frozen": "PASS",
            "software_release": "PASS",
            "golden_142": "PASS",
            "human_golden": "PASS"
        }
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    print(json.dumps({"status": "PASS", "path": str(path), "contract_fingerprint": fp}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
