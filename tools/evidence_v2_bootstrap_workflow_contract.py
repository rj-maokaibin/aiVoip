#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "evidence-v2-production-golden-bootstrap.yml"
BOOTSTRAP = ROOT / "tools" / "evidence_v2_production_golden_bootstrap.py"


def main() -> int:
    text = WORKFLOW.read_text(encoding="utf-8")
    bootstrap = BOOTSTRAP.read_text(encoding="utf-8")
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
        "Verify self-hosted production workspace without mutation",
        "EVIDENCE_V2_GOLDEN_BOOTSTRAP_PRODUCTION_WORKSPACE_BINDING=PASS",
        "python3 tools/source_manifest_gate.py",
        "tools/human_evidence_feishu_live_acceptance.py",
        "BOUND_REAL_GOLDEN_001",
        "strict_validator",
        "default_projection",
        "Verify exact production runtime is already promoted",
        "EVIDENCE_V2_GOLDEN_BOOTSTRAP_PROFILE_BINDING=PASS",
        "profiles/analyzers/voip_v1.yaml|/app/profiles/analyzers/voip_v1.yaml",
        "profiles/pcm/ruijie_aim_diag_v1.yaml|/app/profiles/pcm/ruijie_aim_diag_v1.yaml",
        "tools/evidence_v2_production_golden_bootstrap.py|/tools/evidence_v2_production_golden_bootstrap.py",
        "tools/human_evidence_feishu_live_acceptance.py|/tools/human_evidence_feishu_live_acceptance.py",
        "REAL_OFFLINE_GOLDEN_001_SOURCE_IDENTITY=PASS",
        "GOLDEN_SHA256: b038aa7c9a0644581f2815f654fcdee4620860796382265b178823fccba2e3f0",
        "Bootstrap dedicated production baseline",
        "in_helper=/tmp/human_evidence_feishu_live_acceptance.py",
        "docker cp \"$helper_path\" \"$BACKEND_CID:$in_helper\"",
        "EVIDENCE_V2_GOLDEN_BOOTSTRAP_HELPER_BINDING=PASS",
        "-e PYTHONPATH=/tmp:/app:/tools",
        "Upload sanitized bootstrap evidence",
        "Clean bootstrap scratch without mutating production workspace",
        "EVIDENCE_V2_GOLDEN_BOOTSTRAP_SCRATCH_CLEANUP=PASS production_workspace_mutated=false",
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
        "Verify self-hosted production workspace without mutation",
        "Verify exact source and SHADOW governance",
        "Verify exact production runtime is already promoted",
        "Verify reviewed Real Golden 001 fixture",
        "Bootstrap dedicated production baseline",
        "Upload sanitized bootstrap evidence",
        "Clean bootstrap scratch without mutating production workspace",
    ]
    positions = [text.index(item) for item in ordered]
    assert positions == sorted(positions), f"workflow step order changed: {ordered}"

    bootstrap_pos = text.index("  bootstrap:")
    bootstrap_text = text[bootstrap_pos:]
    forbidden_bootstrap_mutations = [
        "Repair self-hosted workspace before checkout",
        "find /workspace -mindepth 1 -maxdepth 1 -exec rm -rf",
        "chown -R $uid:$gid /workspace",
        "Checkout exact bootstrap source",
        "uses: actions/checkout@v4",
    ]
    present_forbidden = [item for item in forbidden_bootstrap_mutations if item in bootstrap_text]
    assert not present_forbidden, (
        "bootstrap must not mutate or replace the production bind-mount workspace: "
        f"{present_forbidden}"
    )

    workspace_pos = text.index("EVIDENCE_V2_GOLDEN_BOOTSTRAP_PRODUCTION_WORKSPACE_BINDING=PASS")
    runtime_pos = text.index("EVIDENCE_V2_GOLDEN_BOOTSTRAP_RUNTIME_BINDING=PASS")
    profile_pos = text.index("EVIDENCE_V2_GOLDEN_BOOTSTRAP_PROFILE_BINDING=PASS")
    exec_pos = text.index("-e PYTHONPATH=/tmp:/app:/tools")
    assert workspace_pos < profile_pos < runtime_pos < exec_pos, (
        "exact production workspace and live bind mounts must be verified before bootstrap execution"
    )

    helper_copy_pos = text.index("docker cp \"$helper_path\"")
    helper_binding_pos = text.index("EVIDENCE_V2_GOLDEN_BOOTSTRAP_HELPER_BINDING=PASS")
    assert helper_copy_pos < helper_binding_pos < exec_pos, (
        "bootstrap helper must be copied, digest-bound, then exposed through controlled PYTHONPATH before execution"
    )
    assert "sha256sum \"$in_helper\"" in text, "bootstrap helper digest must be verified inside production runtime"

    assert "git_safe rev-parse HEAD" in text
    assert "git_safe rev-parse refs/remotes/origin/master" in text
    assert "git_safe status --porcelain --untracked-files=no" in text
    assert "runtime_sha=\"$(docker exec \"$cid\" sha256sum \"$runtime_path\"" in text
    assert "test \"$runtime_sha\" = \"$host_sha\"" in text

    assert "runtime.get('checks_passed') == runtime.get('checks_total') == 9" in text
    assert "exact.get('passed') is True" in text
    assert "feishu.get('passed') is True" in text
    assert "acceptance.get('stage') == 'SHADOW'" in text
    assert "needs.qualify-auto-bootstrap.result == 'success'" in text

    cleanup_pos = text.index("Clean bootstrap scratch without mutating production workspace")
    cleanup_block = text[cleanup_pos : cleanup_pos + 260]
    assert "if: always()" in cleanup_block, "bootstrap scratch cleanup must always execute"

    bootstrap_required = [
        "def _existing_binding(\n    db,\n    *,\n    case_id: str,\n    evidence_id: str,",
        "exact_analyzers = _exact_successful_analyzers(db, case_id=case_id, evidence_id=evidence_id)",
        "if set(REQUIRED_ANALYZERS) - exact_analyzers:\n        return None",
        "evidence, evidence_created = _ensure_evidence(db, case=case, pcap=pcap)",
        "existing = _existing_binding(\n            db,\n            case_id=str(case.id),\n            evidence_id=str(evidence.id),",
        "analyzer_components = _ensure_analyzers(db, case_id=str(case.id), evidence_id=str(evidence.id))",
        "exact_analyzers = _exact_successful_analyzers(\n            db,\n            case_id=str(case.id),\n            evidence_id=str(evidence.id),",
    ]
    missing_bootstrap = [item for item in bootstrap_required if item not in bootstrap]
    assert not missing_bootstrap, f"missing partial-recovery bootstrap contract markers: {missing_bootstrap}"
    evidence_pos = bootstrap.index("evidence, evidence_created = _ensure_evidence")
    binding_pos = bootstrap.index("existing = _existing_binding")
    analyzer_pos = bootstrap.index("analyzer_components = _ensure_analyzers")
    assert evidence_pos < binding_pos < analyzer_pos, (
        "bootstrap must materialize exact Golden evidence before deciding whether an existing binding is reusable, "
        "and must repair missing exact analyzers before report reprojection"
    )
    assert "EVIDENCE_V2_GOLDEN_BASELINE_ANALYZERS_MISSING" not in bootstrap, (
        "missing analyzers on an otherwise structurally valid binding are a recoverable partial bootstrap state"
    )
    assert "EVIDENCE_V2_GOLDEN_BASELINE_REPORT_BINDING_INVALID" in bootstrap, (
        "structurally invalid report bindings must remain fail-closed"
    )

    print("EVIDENCE_V2_BOOTSTRAP_WORKFLOW_CONTRACT=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
