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


def test_live_acceptance_runtime_contract_is_versioned_and_cached():
    contract = json.loads((ROOT / "deploy/live_acceptance/runtime_contract.json").read_text(encoding="utf-8"))
    assert contract["schema_version"] == 1
    assert contract["contract"] == "voip-live-acceptance-runtime-v1"
    assert contract["runtime_version"] == "1.0.0"
    assert contract["cache_policy"]["reuse"] is True
    assert "base_image_id" in contract["cache_policy"]["key_inputs"]
    assert contract["profiles"]["human-feishu-golden-001"]["golden_sha256"] == "b038aa7c9a0644581f2815f654fcdee4620860796382265b178823fccba2e3f0"


def test_runtime_fingerprint_is_deterministic_and_sensitive_to_inputs():
    runtime = _load(ROOT / "deploy/live_acceptance/runtime.py", "live_acceptance_runtime_test")
    a = runtime.compute_fingerprint("sha256:base", [("a", b"1"), ("b", b"2")])
    b = runtime.compute_fingerprint("sha256:base", [("b", b"2"), ("a", b"1")])
    c = runtime.compute_fingerprint("sha256:base2", [("a", b"1"), ("b", b"2")])
    d = runtime.compute_fingerprint("sha256:base", [("a", b"1"), ("b", b"3")])
    assert a == b
    assert a != c
    assert a != d


def test_runtime_orchestrator_supports_cross_network_database_topology_without_rebuilding_image():
    runtime = _load(ROOT / "deploy/live_acceptance/runtime.py", "live_acceptance_runtime_topology_test")
    text = (ROOT / "deploy/live_acceptance/runtime.py").read_text(encoding="utf-8")
    assert runtime.ORCHESTRATOR_VERSION == "1.2.0"
    assert "_discover_postgres_route" in text
    assert '"additional_networks":database_route.get("additional_networks") or []' in text
    assert '["docker","network","connect",str(network),container_name]' in text
    assert '["docker","create","--name",container_name' in text
    assert '["docker","start","-a",container_name]' in text
    assert '"--network",f"container:{backend_id}"' not in text


def test_database_route_score_recognizes_postgres_across_any_network():
    runtime = _load(ROOT / "deploy/live_acceptance/runtime.py", "live_acceptance_runtime_score_test")
    info = {
        "Name": "/prod-db",
        "Config": {
            "Image": "postgres:16",
            "Env": ["POSTGRES_DB=voip", "POSTGRES_USER=voip"],
            "Labels": {"com.docker.compose.service": "database", "com.docker.compose.project": "aivoip"},
        },
        "NetworkSettings": {
            "Networks": {
                "db-net": {"IPAddress": "172.30.0.2", "Aliases": ["postgres", "prod-db"]}
            }
        },
    }
    assert runtime._postgres_score(info, "postgres") >= 25
    assert runtime._network_aliases(info, "db-net") == {"postgres", "prod-db"}
    assert runtime._compose_project(info) == "aivoip"


def test_release_gate_postgres_is_never_trusted_as_live_database():
    runtime = _load(ROOT / "deploy/live_acceptance/runtime.py", "live_acceptance_runtime_transient_test")
    gate = {
        "Name": "/voip-ai-gate-pg-12345",
        "Config": {"Image": "postgres:16", "Env": ["POSTGRES_DB=voip"], "Labels": {}},
        "NetworkSettings": {"Networks": {"bridge": {"IPAddress": "172.17.0.2", "Aliases": []}}},
    }
    production = {
        "Name": "/aivoip-postgres-1",
        "Config": {"Image": "postgres:16", "Env": ["POSTGRES_DB=voip"], "Labels": {"com.docker.compose.service": "postgres", "com.docker.compose.project": "aivoip"}},
        "NetworkSettings": {"Networks": {"aivoip_default": {"IPAddress": "172.18.0.4", "Aliases": ["postgres"]}}},
    }
    assert runtime._is_transient_postgres_candidate(gate) is True
    assert runtime._is_transient_postgres_candidate(production) is False


def test_preflight_collector_aggregates_all_blockers():
    preflight = _load(ROOT / "deploy/live_acceptance/preflight.py", "live_acceptance_preflight_test")
    collector = preflight.Collector()
    collector.pass_("A", "RUNTIME", "ok")
    collector.block("B", "DATABASE", "bad db")
    collector.block("C", "FEISHU", "bad feishu")
    assert collector.blocking_keys == ["B", "C"]


def test_preflight_has_explicit_database_route_gate():
    text = (ROOT / "deploy/live_acceptance/preflight.py").read_text(encoding="utf-8")
    assert "DATABASE_ROUTE" in text
    assert "candidate_cross_network" in text
    assert "LIVE_ACCEPTANCE_DATABASE_ROUTE_STATUS" in text


def test_human_live_gate_requires_preflight_before_mutation():
    text = (ROOT / "tools/human_evidence_feishu_live_acceptance.py").read_text(encoding="utf-8")
    assert "--preflight-result" in text
    assert "voip-live-acceptance-preflight-v1" in text
    assert "mutation_allowed" in text


def test_preliminary_workflow_uses_reusable_runtime_and_read_only_preflight():
    text = (ROOT / ".github/workflows/preliminary-evidence-v1.yml").read_text(encoding="utf-8")
    prepare = text.index("deploy/live_acceptance/runtime.py prepare")
    preflight = text.index("deploy/live_acceptance/preflight.py")
    mutation = text.index("tools/human_evidence_feishu_live_acceptance.py")
    assert prepare < preflight < mutation
    assert "docker build -t \"$image\" backend" not in text
    assert "live_acceptance_preflight.json" in text
