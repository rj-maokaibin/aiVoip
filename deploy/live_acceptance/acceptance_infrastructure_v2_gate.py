#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _check(condition: bool, name: str, detail: str = "") -> dict:
    return {
        "name": name,
        "status": "PASS" if condition else "FAIL",
        "detail": detail,
    }


def run() -> dict:
    v1_contract_path = ROOT / "deploy/live_acceptance/runtime_contract.json"
    v2_contract_path = ROOT / "deploy/live_acceptance/runtime_contract_v2.json"
    runtime_v2_path = ROOT / "deploy/live_acceptance/runtime_v2.py"
    preflight_v2_path = ROOT / "deploy/live_acceptance/preflight_v2.py"
    mutation_v2_path = ROOT / "tools/human_evidence_feishu_live_acceptance_v2.py"
    workflow_path = ROOT / ".github/workflows/preliminary-evidence-v1.yml"
    docs_path = ROOT / "docs/LIVE_ACCEPTANCE_INFRASTRUCTURE_V2.md"

    v1 = json.loads(v1_contract_path.read_text(encoding="utf-8"))
    v2 = json.loads(v2_contract_path.read_text(encoding="utf-8"))
    runtime_v2 = runtime_v2_path.read_text(encoding="utf-8")
    preflight_v2 = preflight_v2_path.read_text(encoding="utf-8")
    mutation_v2 = mutation_v2_path.read_text(encoding="utf-8")
    workflow = workflow_path.read_text(encoding="utf-8")

    checks = [
        _check(v1.get("contract") == "voip-live-acceptance-runtime-v1", "V1_CONTRACT_PRESERVED"),
        _check(v1.get("runtime_version") == "1.0.0", "V1_RUNTIME_VERSION_PRESERVED"),
        _check(v2.get("schema_version") == 2, "V2_SCHEMA_VERSION"),
        _check(v2.get("contract") == "voip-live-acceptance-runtime-v2", "V2_RUNTIME_CONTRACT"),
        _check(v2.get("runtime_version") == "2.0.0", "V2_RUNTIME_VERSION"),
        _check(v2.get("acceptance_infrastructure_version") == "2.0", "V2_INFRASTRUCTURE_VERSION"),
        _check((v2.get("compatibility") or {}).get("v1_runtime_preserved") is True, "V1_RUNTIME_COMPATIBILITY"),
        _check((v2.get("compatibility") or {}).get("v1_live_evidence_remains_valid") is True, "V1_EVIDENCE_COMPATIBILITY"),
        _check("voip-live-acceptance-runtime-context-v2" in runtime_v2, "V2_RUNTIME_CONTEXT"),
        _check("io.ruijie.voip.live_acceptance.contract={RUNTIME_CONTRACT}" in runtime_v2, "V2_IMAGE_CONTRACT_LABEL"),
        _check("voip-live-acceptance-preflight-v2" in preflight_v2, "V2_PREFLIGHT_CONTRACT"),
        _check("legacy.PREFLIGHT_CONTRACT = V2_PREFLIGHT_CONTRACT" in mutation_v2, "V2_MUTATION_FAIL_CLOSED_BRIDGE"),
        _check("legacy.PREFLIGHT_CONTRACT = original_contract" in mutation_v2, "V2_MUTATION_BRIDGE_RESTORE"),
        _check("live-feishu-acceptance:" not in workflow, "NORMAL_PR_NO_LIVE_MUTATION"),
        _check("tools/human_evidence_feishu_live_acceptance.py" not in workflow, "NORMAL_PR_NO_LEGACY_MUTATION"),
        _check(docs_path.is_file(), "V2_DOD_DOCUMENTED"),
    ]
    failed = [row["name"] for row in checks if row["status"] != "PASS"]
    return {
        "schema_version": 1,
        "contract": "voip-acceptance-infrastructure-v2-gate-v1",
        "status": "PASS" if not failed else "FAIL",
        "failed_checks": failed,
        "checks": checks,
    }


def main() -> int:
    payload = run()
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
