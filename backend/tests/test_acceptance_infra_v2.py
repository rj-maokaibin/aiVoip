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


def test_acceptance_v2_contract_and_golden_are_versioned():
    contract = json.loads((ROOT / "deploy/acceptance_v2/contract.json").read_text(encoding="utf-8"))
    golden = json.loads((ROOT / "golden_registry/real_offline_001/manifest.json").read_text(encoding="utf-8"))
    assert contract["contract"] == "voip-acceptance-infrastructure-v2"
    assert contract["version"] == "2.0.0"
    assert contract["root_default"] == "/opt/voip-acceptance"
    assert golden["golden_id"] == "REAL_OFFLINE_GOLDEN_001"
    assert golden["artifacts"]["pcap"]["sha256"] == "b038aa7c9a0644581f2815f654fcdee4620860796382265b178823fccba2e3f0"
    assert golden["source_policy"] == "EXTERNAL_REGISTRY_OR_PERSISTENT_CACHE_ONLY"


def test_golden_cache_path_is_persistent_not_tmp():
    golden_module = _load(ROOT / "tools/acceptance_golden.py", "acceptance_golden_test")
    manifest = golden_module.load_manifest(ROOT / "golden_registry/real_offline_001/manifest.json")
    path = golden_module.cache_path(Path("/opt/voip-acceptance"), manifest)
    assert str(path).startswith("/opt/voip-acceptance/golden-cache/")
    assert "/tmp/" not in str(path)


def test_prepared_runtime_fingerprint_covers_python_frontend_and_image_contract():
    runtime = _load(ROOT / "tools/acceptance_runtime.py", "acceptance_runtime_test")
    fp = runtime.fingerprint()
    assert len(fp) == 64
    assert "backend/requirements.txt" in runtime.INPUTS
    assert "frontend/package-lock.json" in runtime.INPUTS
    assert "deploy/acceptance_v2/Dockerfile" in runtime.INPUTS


def test_software_evidence_fingerprint_covers_gate_contracts():
    evidence = _load(ROOT / "tools/acceptance_evidence.py", "acceptance_evidence_test")
    fp = evidence.contract_fingerprint()
    assert len(fp) == 64
    assert ".github/workflows/preliminary-evidence-v1.yml" in evidence.CONTRACT_INPUTS
    assert "golden_registry/real_offline_001/manifest.json" in evidence.CONTRACT_INPUTS
    assert "backend/requirements.txt" in evidence.CONTRACT_INPUTS


def test_pr_workflow_fail_fast_without_network_probe_becoming_a_gate():
    text = (ROOT / ".github/workflows/preliminary-evidence-v1.yml").read_text(encoding="utf-8")
    assert "infra-preflight:" in text
    assert "Observe host network before checkout" in text
    assert "continue-on-error: true" in text
    assert "Checkout exact head attempt 2" in text
    assert "Runner Doctor - merge gate prerequisites only" in text
    assert "--require-runtime" in text
    assert "--deep-network" not in text
    assert "acceptance_golden.py ensure" in text
    assert "acceptance_runtime.py env" in text
    assert "acceptance_evidence.py check" in text
    assert "acceptance_evidence.py record" in text
    assert "/tmp/tcpdump-2026-08-14.pcap" not in text
    assert "apt-get download" not in text
    assert "apt-get install" not in text


def test_release_gate_can_consume_prepared_offline_runtime():
    text = (ROOT / "tools/voip_ai_release_gate.sh").read_text(encoding="utf-8")
    assert "VOIP_AI_PREPARED_VENV" in text
    assert "prepared Python runtime" in text
    assert "VOIP_AI_OFFLINE_GATE" in text
    assert "npm ci --offline" in text
    assert 'if [[ "${VOIP_AI_OFFLINE_GATE:-0}" != "1" ]]' in text


def test_acceptance_stack_is_isolated_and_ephemeral():
    text = (ROOT / "deploy/acceptance_v2/docker-compose.yml").read_text(encoding="utf-8")
    assert "name: voip-acceptance-v2" in text
    assert "tmpfs:" in text
    assert "aivoip" not in text
    assert "POSTGRES_DB: voip_acceptance" in text


def test_bootstrap_is_only_tmp_migration_compatibility_path():
    text = (ROOT / "tools/bootstrap_acceptance_host.sh").read_text(encoding="utf-8")
    assert "/tmp/tcpdump-2026-08-14.pcap" in text
    assert "One-time migration compatibility only" in text
    assert "acceptance_runtime.py\" prepare" in text
    workflow = (ROOT / ".github/workflows/preliminary-evidence-v1.yml").read_text(encoding="utf-8")
    assert "/tmp/tcpdump-2026-08-14.pcap" not in workflow
