#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
V1_PREFLIGHT_PATH = ROOT / "deploy/live_acceptance/preflight.py"
DEFAULT_CONTRACT = ROOT / "deploy/live_acceptance/runtime_contract_v2.json"
RUNTIME_CONTRACT = "voip-live-acceptance-runtime-v2"
PREFLIGHT_CONTRACT = "voip-live-acceptance-preflight-v2"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"MODULE_LOAD_FAILED:{path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


v1 = _load_module(V1_PREFLIGHT_PATH, "voip_live_acceptance_preflight_v1_compat")


def _load_contract(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("contract") != RUNTIME_CONTRACT:
        raise RuntimeError("LIVE_ACCEPTANCE_PREFLIGHT_V2_RUNTIME_CONTRACT_INVALID")
    if int(data.get("schema_version") or 0) != 2:
        raise RuntimeError("LIVE_ACCEPTANCE_PREFLIGHT_V2_SCHEMA_UNSUPPORTED")
    return data


async def run(contract: dict, profile_name: str) -> dict:
    payload = await v1.run(contract, profile_name)
    payload["schema_version"] = 2
    payload["contract"] = PREFLIGHT_CONTRACT
    payload["acceptance_infrastructure_version"] = contract.get("acceptance_infrastructure_version", "2.0")
    payload["compatibility"] = {
        "v1_probe_implementation_reused": True,
        "v1_live_evidence_remains_valid": True,
        "mutation_bridge": "tools/human_evidence_feishu_live_acceptance_v2.py",
    }
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Aggregated read-only preflight for Acceptance Infrastructure V2")
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    parser.add_argument("--profile", default="base")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    payload = asyncio.run(run(_load_contract(args.contract.resolve()), args.profile))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
