#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "evidence-v2-production-golden-bootstrap.yml"


def main() -> int:
    text = WORKFLOW.read_text(encoding="utf-8")
    required = [
        "Repair self-hosted workspace before checkout",
        "EVIDENCE_V2_GOLDEN_BOOTSTRAP_RUNNER_WORKSPACE_REPAIR=PASS",
        "Checkout exact bootstrap source",
        "python3 tools/source_manifest_gate.py",
        "BOUND_REAL_GOLDEN_001",
        "strict_validator",
        "default_projection",
        "Verify exact production runtime is already promoted",
        "REAL_OFFLINE_GOLDEN_001_SOURCE_IDENTITY=PASS",
        "GOLDEN_SHA256: b038aa7c9a0644581f2815f654fcdee4620860796382265b178823fccba2e3f0",
        "Bootstrap dedicated production baseline",
        "Upload sanitized bootstrap evidence",
        "Restore self-hosted runner workspace ownership",
        "EVIDENCE_V2_GOLDEN_BOOTSTRAP_RUNNER_WORKSPACE_RESTORE=PASS",
    ]
    missing = [item for item in required if item not in text]
    assert not missing, f"missing workflow contract markers: {missing}"

    ordered = [
        "Repair self-hosted workspace before checkout",
        "Checkout exact bootstrap source",
        "Verify exact source and SHADOW governance",
        "Verify exact production runtime is already promoted",
        "Verify reviewed Real Golden 001 fixture",
        "Bootstrap dedicated production baseline",
        "Upload sanitized bootstrap evidence",
        "Restore self-hosted runner workspace ownership",
    ]
    positions = [text.index(item) for item in ordered]
    assert positions == sorted(positions), f"workflow step order changed: {ordered}"

    restore_pos = text.index("Restore self-hosted runner workspace ownership")
    restore_block = text[restore_pos : restore_pos + 260]
    assert "if: always()" in restore_block, "runner ownership restore must always execute"

    print("EVIDENCE_V2_BOOTSTRAP_WORKFLOW_CONTRACT=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
