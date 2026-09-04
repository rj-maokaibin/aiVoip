from __future__ import annotations

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_derived_source_manifest_does_not_mutate_tracked_source(tmp_path: Path) -> None:
    before_head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    before_status = subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT, text=True
    ).strip()
    assert before_status == ""

    expected = tmp_path / "source_manifest_expected.json"
    subprocess.run(
        ["python3", "tools/source_manifest_gate.py", "--write", str(expected)],
        cwd=ROOT,
        check=True,
        text=True,
    )
    subprocess.run(
        ["python3", "tools/source_manifest_gate.py", "--expected", str(expected)],
        cwd=ROOT,
        check=True,
        text=True,
    )

    payload = json.loads(expected.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["file_count"] > 0
    assert len(payload["aggregate_sha256"]) == 64
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip() == before_head
    assert subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT, text=True
    ).strip() == ""


def test_authoritative_workflows_use_derived_manifest_artifact() -> None:
    source = (ROOT / ".github/workflows/source-manifest-gate.yml").read_text(encoding="utf-8")
    acceptance = (ROOT / ".github/workflows/prd-spec-v1-release.yml").read_text(encoding="utf-8")
    production = (ROOT / ".github/workflows/production-deploy.yml").read_text(encoding="utf-8")

    assert "source_manifest_expected.json" in source
    assert "--write \"$RUNNER_TEMP/source_manifest_expected.json\"" in source
    assert "SOURCE_MANIFEST_DERIVED_ARTIFACT=PASS mutation=false" in source
    assert "release/source_manifest.json" not in source

    assert "source_manifest_expected.json" in acceptance
    assert "SOURCE_MANIFEST_ACCEPTANCE_BINDING=PASS mode=DERIVED_ARTIFACT" in acceptance

    assert "source_manifest_expected.json" in production
    assert "PRODUCTION_SOURCE_MANIFEST_BINDING=PASS mode=DERIVED_ARTIFACT" in production
    assert "release/source_manifest.json" not in production


def test_full_acceptance_reuses_one_python_runtime_and_bounds_network() -> None:
    workflow = (ROOT / ".github/workflows/prd-spec-v1-release.yml").read_text(encoding="utf-8")
    release_gate = (ROOT / "tools/voip_ai_release_gate.sh").read_text(encoding="utf-8")
    frozen_gate = (ROOT / "tools/preliminary_evidence_v1_gate.sh").read_text(encoding="utf-8")
    helper = (ROOT / "tools/ci_dependency_runtime.sh").read_text(encoding="utf-8")

    shared = "/tmp/voip-ai-acceptance-runtime-${{ github.run_id }}-${{ github.run_attempt }}"
    assert workflow.count(shared) == 2
    assert "ci_prepare_python_runtime" in frozen_gate
    assert "ci_prepare_python_runtime" in release_gate
    assert "pip install --upgrade pip" not in frozen_gate
    assert "pip install --upgrade pip" not in release_gate

    assert "VOIP_NPM_CI_TIMEOUT_SECONDS:-30" in helper
    assert "VOIP_NPM_AUDIT_TIMEOUT_SECONDS:-30" in helper
    assert "npm ci --prefer-offline --no-audit --no-fund" in helper
    assert "npm audit --audit-level=low" in helper
    assert "PERF_PHASE_V3" in (ROOT / "tools/cicd_performance_v3.py").read_text(encoding="utf-8")


def test_frontend_acceptance_is_exact_sha_github_hosted_and_reused() -> None:
    workflow = (ROOT / ".github/workflows/prd-spec-v1-release.yml").read_text(encoding="utf-8")
    release_gate = (ROOT / "tools/voip_ai_release_gate.sh").read_text(encoding="utf-8")

    assert "frontend-acceptance:" in workflow
    frontend_section = workflow.split("  frontend-acceptance:", 1)[1].split("  full-software-acceptance:", 1)[0]
    assert "runs-on: ubuntu-latest" in frontend_section
    assert "ref: ${{ env.EXPECTED_SHA }}" in frontend_section
    assert "frontend-acceptance-${{ env.EXPECTED_SHA }}" in frontend_section
    assert "ci_npm_ci" in frontend_section
    assert "ci_npm_audit" in frontend_section
    assert "FRONTEND_ACCEPTANCE=PASS" in frontend_section

    full_section = workflow.split("  full-software-acceptance:", 1)[1]
    assert "needs.frontend-acceptance.result == 'success'" in full_section
    assert "Download exact-SHA frontend acceptance evidence" in full_section
    assert "FRONTEND_ACCEPTANCE_EVIDENCE=PASS source=GITHUB_HOSTED" in full_section

    assert "FRONTEND_ACCEPTANCE_EVIDENCE=PASS" in release_gate
    assert "frontend_acceptance_result.json" in release_gate
    assert "npm ci" not in release_gate
    assert "npm audit" not in release_gate
