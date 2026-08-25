from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_v2_contract_is_formal_and_v1_contract_is_preserved():
    v1 = json.loads((ROOT / "deploy/live_acceptance/runtime_contract.json").read_text(encoding="utf-8"))
    v2 = json.loads((ROOT / "deploy/live_acceptance/runtime_contract_v2.json").read_text(encoding="utf-8"))
    assert v1["schema_version"] == 1
    assert v1["contract"] == "voip-live-acceptance-runtime-v1"
    assert v1["runtime_version"] == "1.0.0"
    assert v2["schema_version"] == 2
    assert v2["contract"] == "voip-live-acceptance-runtime-v2"
    assert v2["runtime_version"] == "2.0.0"
    assert v2["acceptance_infrastructure_version"] == "2.0"
    assert v2["compatibility"]["v1_runtime_preserved"] is True
    assert v2["compatibility"]["v1_preflight_preserved"] is True
    assert v2["compatibility"]["v1_live_evidence_remains_valid"] is True
    assert "READ_ONLY_LIVE_PREFLIGHT" in v2["acceptance_gates"]
    assert "NORMAL_PR_CI_NON_MUTATING" in v2["safety_invariants"]


def test_v2_runtime_contract_loader_and_fingerprint_are_version_scoped():
    v1_runtime = _load(ROOT / "deploy/live_acceptance/runtime.py", "live_acceptance_runtime_v1_for_v2_test")
    v2_runtime = _load(ROOT / "deploy/live_acceptance/runtime_v2.py", "live_acceptance_runtime_v2_test")
    contract = v2_runtime._load_contract(ROOT / "deploy/live_acceptance/runtime_contract_v2.json")
    assert contract["contract"] == v2_runtime.RUNTIME_CONTRACT
    assert v2_runtime.ORCHESTRATOR_VERSION == "2.0.0"
    a = v2_runtime.compute_fingerprint("sha256:base", [("a", b"1"), ("b", b"2")])
    b = v2_runtime.compute_fingerprint("sha256:base", [("b", b"2"), ("a", b"1")])
    c = v2_runtime.compute_fingerprint("sha256:base2", [("a", b"1"), ("b", b"2")])
    legacy = v1_runtime.compute_fingerprint("sha256:base", [("a", b"1"), ("b", b"2")])
    assert a == b
    assert a != c
    assert a != legacy


def test_v2_runtime_context_is_not_confused_with_v1_context(tmp_path):
    runtime = _load(ROOT / "deploy/live_acceptance/runtime_v2.py", "live_acceptance_runtime_v2_context_test")
    path = tmp_path / "context.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "contract": "voip-live-acceptance-runtime-context-v2",
                "runtime_contract": "voip-live-acceptance-runtime-v2",
            }
        ),
        encoding="utf-8",
    )
    loaded = runtime._load_context(path)
    assert loaded["runtime_contract"] == "voip-live-acceptance-runtime-v2"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "contract": "voip-live-acceptance-runtime-context-v1",
                "runtime_contract": "voip-live-acceptance-runtime-v1",
            }
        ),
        encoding="utf-8",
    )
    try:
        runtime._load_context(path)
    except RuntimeError as exc:
        assert "V2_CONTEXT_INVALID" in str(exc)
    else:
        raise AssertionError("V2 runtime accepted a V1 context")


def test_v2_runtime_reuses_only_guarded_v1_runtime_primitives():
    text = (ROOT / "deploy/live_acceptance/runtime_v2.py").read_text(encoding="utf-8")
    assert "v1._discover_real_backend" in text
    assert "v1._discover_postgres_route" in text
    assert "v1._recover_database" in text
    assert "v1._run_in_runtime" in text
    assert "v1._load_context = _load_context" in text
    assert "io.ruijie.voip.live_acceptance.contract={RUNTIME_CONTRACT}" in text
    assert '"schema_version": 2' in text
    assert '"contract": CONTEXT_CONTRACT' in text


def test_v2_preflight_wraps_existing_read_only_probe_set_without_weakening_identity():
    preflight = _load(ROOT / "deploy/live_acceptance/preflight_v2.py", "live_acceptance_preflight_v2_test")
    contract = preflight._load_contract(ROOT / "deploy/live_acceptance/runtime_contract_v2.json")
    assert contract["contract"] == preflight.RUNTIME_CONTRACT
    text = (ROOT / "deploy/live_acceptance/preflight_v2.py").read_text(encoding="utf-8")
    assert "payload = await v1.run(contract, profile_name)" in text
    assert 'payload["contract"] = PREFLIGHT_CONTRACT' in text
    assert 'payload["schema_version"] = 2' in text
    assert "voip-live-acceptance-preflight-v2" in text


def test_v2_live_mutation_entrypoint_keeps_legacy_helper_fail_closed():
    text = (ROOT / "tools/human_evidence_feishu_live_acceptance_v2.py").read_text(encoding="utf-8")
    legacy = (ROOT / "tools/human_evidence_feishu_live_acceptance.py").read_text(encoding="utf-8")
    assert 'V2_PREFLIGHT_CONTRACT = "voip-live-acceptance-preflight-v2"' in text
    assert "legacy.PREFLIGHT_CONTRACT = V2_PREFLIGHT_CONTRACT" in text
    assert "legacy.PREFLIGHT_CONTRACT = original_contract" in text
    assert 'result["contract"] = V2_LIVE_CONTRACT' in text
    assert 'PREFLIGHT_CONTRACT = "voip-live-acceptance-preflight-v1"' in legacy
    assert 'payload.get("status") != "PASS" or payload.get("mutation_allowed") is not True' in legacy
    assert "LIVE_ACCEPTANCE_PREFLIGHT_REVISION_MISMATCH" in legacy


def test_v2_static_gate_and_normal_pr_non_mutation_contract():
    gate = _load(ROOT / "deploy/live_acceptance/acceptance_infrastructure_v2_gate.py", "acceptance_infrastructure_v2_gate_test")
    payload = gate.run()
    assert payload["contract"] == "voip-acceptance-infrastructure-v2-gate-v1"
    assert payload["status"] == "PASS", payload
    workflow = (ROOT / ".github/workflows/preliminary-evidence-v1.yml").read_text(encoding="utf-8")
    assert "live-feishu-acceptance:" not in workflow
    assert "tools/human_evidence_feishu_live_acceptance.py" not in workflow


def test_v2_definition_of_done_is_documented():
    doc = (ROOT / "docs/LIVE_ACCEPTANCE_INFRASTRUCTURE_V2.md").read_text(encoding="utf-8")
    assert "Acceptance Infrastructure V2" in doc
    assert "Definition of Done" in doc
    assert "V1" in doc and "兼容" in doc
    assert "Real Offline Golden #001" in doc
    assert "显式" in doc
