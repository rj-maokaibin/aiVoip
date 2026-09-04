#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "evidence-v2-production-golden-bootstrap.yml"


def main() -> int:
    text = WORKFLOW.read_text(encoding="utf-8")
    required = [
        "workflow_dispatch:",
        "workflow_run:",
        "Production Deploy",
        "actions: read",
        "qualify-auto-bootstrap:",
        "github.event.workflow_run.head_branch == 'master'",
        "github.event.workflow_run.conclusion == 'failure'",
        "Checkout current master for anti-stale binding",
        "Download triggering Production Deploy evidence",
        "NO_BOUND_REAL_GOLDEN_001_CASE_EVIDENCE",
        "EVIDENCE_V2_AUTO_BOOTSTRAP_QUALIFICATION=PASS",
        "github.event_name == 'workflow_run' && github.event.workflow_run.head_sha || github.sha",
        "Repair self-hosted workspace before checkout",
        "EVIDENCE_V2_GOLDEN_BOOTSTRAP_RUNNER_WORKSPACE_REPAIR=PASS",
        "Checkout exact bootstrap source",
        "python3 tools/source_manifest_gate.py",
        "tools/human_evidence_feishu_live_acceptance.py",
        "BOUND_REAL_GOLDEN_001",
        "strict_validator",
        "default_projection",
        "Verify exact production runtime is already promoted",
        "REAL_OFFLINE_GOLDEN_001_SOURCE_IDENTITY=PASS",
        "GOLDEN_SHA256: b038aa7c9a0644581f2815f654fcdee4620860796382265b178823fccba2e3f0",
        "Bootstrap dedicated production baseline",
        "in_helper=/tmp/human_evidence_feishu_live_acceptance.py",
        "docker cp tools/human_evidence_feishu_live_acceptance.py \"$BACKEND_CID:$in_helper\"",
        "EVIDENCE_V2_GOLDEN_BOOTSTRAP_HELPER_BINDING=PASS",
        "-e PYTHONPATH=/tmp:/app:/tools",
        "Upload sanitized bootstrap evidence",
        "Restore self-hosted runner workspace ownership",
        "EVIDENCE_V2_GOLDEN_BOOTSTRAP_RUNNER_WORKSPACE_RESTORE=PASS",
    ]
    missing = [item for item in required if item not in text]
    assert not missing, f"missing workflow contract markers: {missing}"

    qualification_ordered = [
        "Checkout current master for anti-stale binding",
        "Verify triggering run is still exact current master",
        "Download triggering Production Deploy evidence",
        "Qualify only expected missing-golden fail-closed",
    ]
    qualification_positions = [text.index(item) for item in qualification_ordered]
    assert qualification_positions == sorted(qualification_positions), (
        f"auto-bootstrap qualification order changed: {qualification_ordered}"
    )

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

    helper_copy_pos = text.index("docker cp tools/human_evidence_feishu_live_acceptance.py")
    helper_binding_pos = text.index("EVIDENCE_V2_GOLDEN_BOOTSTRAP_HELPER_BINDING=PASS")
    bootstrap_exec_pos = text.index("-e PYTHONPATH=/tmp:/app:/tools")
    assert helper_copy_pos < helper_binding_pos < bootstrap_exec_pos, (
        "bootstrap helper must be copied, digest-bound, then exposed through controlled PYTHONPATH before execution"
    )
    assert "sha256sum \"$in_helper\"" in text, "bootstrap helper digest must be verified inside production runtime"

    assert "runtime.get('checks_passed') == runtime.get('checks_total') == 9" in text
    assert "exact.get('passed') is True" in text
    assert "feishu.get('passed') is True" in text
    assert "acceptance.get('stage') == 'SHADOW'" in text
    assert "needs.qualify-auto-bootstrap.result == 'success'" in text

    restore_pos = text.index("Restore self-hosted runner workspace ownership")
    restore_block = text[restore_pos : restore_pos + 260]
    assert "if: always()" in restore_block, "runner ownership restore must always execute"

    print("EVIDENCE_V2_BOOTSTRAP_WORKFLOW_CONTRACT=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
