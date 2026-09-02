from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "deploy" / "exact_source_binding_gate.py"
spec = importlib.util.spec_from_file_location("exact_source_binding_gate", MODULE_PATH)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def _base_snapshot() -> dict:
    revision = "a" * 40
    return {
        "expected_revision": revision,
        "git_head": revision,
        "tracked_dirty": False,
        "source_manifest_status": "PASS",
        "source_manifest_aggregate_sha256": "b" * 64,
        "services": {
            service: {
                "container_count": 1,
                "container_revision": revision,
                "image_revision": revision,
            }
            for service in module.APP_SERVICES
        },
        "backend_health_revision": revision,
        "runtime_evidence_passed": True,
        "runtime_evidence_revision": revision,
        "feishu_required": True,
        "feishu_evidence_passed": True,
        "feishu_evidence_revision": revision,
    }


def test_exact_source_binding_accepts_five_way_match():
    assert module.evaluate_snapshot(_base_snapshot(), phase="runtime") == []


def test_exact_source_binding_rejects_source_revision_mismatch():
    snapshot = _base_snapshot()
    snapshot["git_head"] = "c" * 40
    errors = module.evaluate_snapshot(snapshot, phase="source")
    assert any(x.startswith("GIT_HEAD_MISMATCH") for x in errors)


def test_exact_source_binding_rejects_image_revision_mismatch():
    snapshot = _base_snapshot()
    snapshot["services"]["diagnosis-worker"]["image_revision"] = "c" * 40
    errors = module.evaluate_snapshot(snapshot, phase="runtime")
    assert any(x.startswith("IMAGE_REVISION_MISMATCH:diagnosis-worker") for x in errors)


def test_exact_source_binding_rejects_container_revision_mismatch():
    snapshot = _base_snapshot()
    snapshot["services"]["backend"]["container_revision"] = "c" * 40
    errors = module.evaluate_snapshot(snapshot, phase="runtime")
    assert any(x.startswith("CONTAINER_REVISION_MISMATCH:backend") for x in errors)


def test_exact_source_binding_rejects_health_and_runtime_evidence_mismatch():
    snapshot = _base_snapshot()
    snapshot["backend_health_revision"] = "c" * 40
    snapshot["runtime_evidence_revision"] = "c" * 40
    errors = module.evaluate_snapshot(snapshot, phase="runtime")
    assert any(x.startswith("BACKEND_HEALTH_REVISION_MISMATCH") for x in errors)
    assert any(x.startswith("RUNTIME_EVIDENCE_REVISION_MISMATCH") for x in errors)


def test_exact_source_binding_requires_feishu_evidence_when_live():
    snapshot = _base_snapshot()
    snapshot["feishu_evidence_passed"] = False
    snapshot["feishu_evidence_revision"] = None
    errors = module.evaluate_snapshot(snapshot, phase="runtime")
    assert "FEISHU_EVIDENCE_NOT_PASS" in errors
    assert any(x.startswith("FEISHU_EVIDENCE_REVISION_MISMATCH") for x in errors)


def test_exact_source_binding_rejects_dirty_tracked_source_and_manifest_drift():
    snapshot = _base_snapshot()
    snapshot["tracked_dirty"] = True
    snapshot["source_manifest_status"] = "FAIL"
    errors = module.evaluate_snapshot(snapshot, phase="source")
    assert "TRACKED_WORKTREE_DIRTY" in errors
    assert "SOURCE_MANIFEST_FAIL" in errors
