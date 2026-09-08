from __future__ import annotations

import ast
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
CONSOLIDATED = WORKFLOWS / "consolidated-exact-head-validation.yml"

LEGACY = {
    "current-dut-sip-identity-read-probe.yml",
    "current-dut-web-auth-symbol-probe.yml",
    "current-dut-web-authres-flow-probe.yml",
    "current-dut-web-checkpasswd-flow-probe.yml",
    "current-dut-web-checkpasswd-probe.yml",
    "current-dut-web-login-flow-probe.yml",
    "current-pbx-db-read-probe.yml",
    "current-pbx-provider-source-probe-v2.yml",
    "current-pbx-provider-source-probe.yml",
    "current-pr-d-identity-probe.yml",
    "current-web-auth-runtime-diagnostic.yml",
    "current-web-auth-source-probe.yml",
    "current-web-credential-source-probe.yml",
    "current-web-live-prereq-probe.yml",
}
LEGACY |= {
    "prd-spec-v1-release.yml",
    "preliminary-evidence-v1.yml",
    "source-manifest-gate.yml",
}


def _on(document: dict) -> dict:
    return document.get("on") or document.get(True) or {}


def test_consolidated_workflow_is_the_only_pr_automatic_gate() -> None:
    document = yaml.safe_load(CONSOLIDATED.read_text(encoding="utf-8"))
    assert document["name"] == "Consolidated Exact-Head Validation"
    assert "pull_request" in _on(document)
    jobs = document["jobs"]
    assert list(jobs) == ["source-and-frontend", "self-hosted-validation", "preliminary-authority"]
    self_hosted = [name for name, job in jobs.items() if "self-hosted" in str(job.get("runs-on"))]
    assert self_hosted == ["self-hosted-validation"]


def test_legacy_exact_head_workflows_are_manual_only() -> None:
    for filename in sorted(LEGACY):
        document = yaml.safe_load((WORKFLOWS / filename).read_text(encoding="utf-8"))
        assert set(_on(document)) == {"workflow_dispatch"}, filename


def test_consolidated_workflow_keeps_all_frozen_release_gates() -> None:
    text = CONSOLIDATED.read_text(encoding="utf-8")
    for token in (
        "source_manifest_gate.py",
        "production_compose_config_gate.sh",
        "preliminary_evidence_v1_gate.sh",
        "voip_ai_release_gate.sh",
        "offline_analysis_golden_replay.py",
        "human_evidence_real_golden_gate.py",
        "full_acceptance_evidence_gate.py",
        "consolidated_exact_head_probes.py",
    ):
        assert token in text


def test_consolidated_probe_has_no_mutation_api_path() -> None:
    source = (ROOT / "tools" / "consolidated_exact_head_probes.py").read_text(encoding="utf-8")
    ast.parse(source)
    for forbidden in (
        "configure_voip_bundle(",
        "ConfigFrameworkExecutor.set(",
        "extension->save(",
        "extension->delete(",
        "create_extension(",
        "delete_extension(",
    ):
        assert forbidden not in source
