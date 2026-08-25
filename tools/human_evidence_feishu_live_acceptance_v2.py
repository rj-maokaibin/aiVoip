#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEGACY_HELPER = ROOT / "tools/human_evidence_feishu_live_acceptance.py"
V2_PREFLIGHT_CONTRACT = "voip-live-acceptance-preflight-v2"
V2_LIVE_CONTRACT = "human-evidence-feishu-live-acceptance-v2"


def _load_legacy():
    spec = importlib.util.spec_from_file_location("human_evidence_feishu_live_acceptance_v1_compat", LEGACY_HELPER)
    if spec is None or spec.loader is None:
        raise RuntimeError("LEGACY_LIVE_ACCEPTANCE_HELPER_LOAD_FAILED")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


legacy = _load_legacy()


async def run(result_path: Path, preflight_path: Path) -> dict:
    original_contract = legacy.PREFLIGHT_CONTRACT
    legacy.PREFLIGHT_CONTRACT = V2_PREFLIGHT_CONTRACT
    try:
        result = await legacy.run(result_path, preflight_path)
    finally:
        legacy.PREFLIGHT_CONTRACT = original_contract

    legacy_contract = result.get("contract")
    result["contract"] = V2_LIVE_CONTRACT
    result["acceptance_infrastructure_version"] = "2.0"
    result["legacy_projection_core_contract"] = legacy_contract
    result["preflight_contract"] = V2_PREFLIGHT_CONTRACT
    result["compatibility_mode"] = "V2_GUARD_WITH_V1_PROJECTION_CORE"
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Explicit Human Feishu live acceptance entrypoint for Acceptance Infrastructure V2")
    parser.add_argument("--result", default="validation/human_evidence_feishu_live_acceptance_v2.json")
    parser.add_argument("--preflight-result", required=True)
    args = parser.parse_args()

    path = Path(args.result)
    path.parent.mkdir(parents=True, exist_ok=True)
    result = asyncio.run(run(path, Path(args.preflight_result)))
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
