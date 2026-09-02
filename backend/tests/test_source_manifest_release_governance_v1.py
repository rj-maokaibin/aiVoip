from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "tools" / "source_manifest_gate.py"
spec = importlib.util.spec_from_file_location("source_manifest_gate", MODULE_PATH)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_release_governance_workflows_are_source_manifest_inputs():
    required = {
        ".github/workflows/production-deploy.yml",
        ".github/workflows/source-manifest-gate.yml",
    }
    assert required.issubset(set(module.INCLUDE_FILES))


def test_exact_source_binding_gate_is_manifest_covered_by_deploy_directory():
    paths = {str(path.relative_to(ROOT)).replace("\\", "/") for path in module.files()}
    assert "deploy/exact_source_binding_gate.py" in paths


def test_release_source_manifest_excludes_only_manifest_itself():
    assert "release/source_manifest.json" in module.EXCLUDE_FILES
    assert ".github/workflows/production-deploy.yml" not in module.EXCLUDE_FILES
    assert ".github/workflows/source-manifest-gate.yml" not in module.EXCLUDE_FILES
