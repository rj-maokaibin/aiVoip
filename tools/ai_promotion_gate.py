#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from tools.ai_eval_gate import evaluate as evaluate_contract


def evaluate_promotion(*, manifest_path: Path, quality_report_path: Path) -> dict:
    contract = evaluate_contract(manifest_path)
    payload = json.loads(quality_report_path.read_text(encoding="utf-8"))
    quality = payload.get("report") if payload.get("schema_version") == "ai-model-eval-run-v2" else payload
    errors: list[str] = []
    if contract.get("status") != "PASS":
        errors.append("AI_EVAL_CONTRACT_GATE_FAILED")
    if quality.get("schema_version") != "ai-model-quality-report-v2":
        errors.append("AI_MODEL_QUALITY_SCHEMA_INVALID")
    if quality.get("status") != "PASS":
        errors.append(f"AI_MODEL_QUALITY_NOT_PASS:{quality.get('status')}")
    if not (quality.get("gate") or {}).get("promotion_eligible"):
        errors.append("AI_MODEL_NOT_PROMOTION_ELIGIBLE")
    hard_zero = quality.get("hard_zero_metrics") or {}
    if any(int(value or 0) for value in hard_zero.values()):
        errors.append("AI_HARD_ZERO_VIOLATION")
    if not (quality.get("gate") or {}).get("audit_coverage_complete"):
        errors.append("AI_AUDIT_COVERAGE_INCOMPLETE")

    return {
        "schema_version": "ai-promotion-gate-v1",
        "status": "PASS" if not errors else "BLOCKED",
        "promotion_stage_allowed": "CONTROLLED_PLANNER" if not errors else "SHADOW",
        "formal_reasoner_authority": "DETERMINISTIC_ONLY",
        "raw_device_command_authority": "FORBIDDEN",
        "ai_only_root_cause_confirmation": "FORBIDDEN",
        "contract_gate": contract,
        "quality_gate": quality,
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Promote AI only after real verified Eval passes")
    parser.add_argument("--manifest", default="golden_cases/ai_shadow_eval_v1.json")
    parser.add_argument("--quality-report", required=True)
    parser.add_argument("--out", default="validation/ai_promotion_gate.json")
    args = parser.parse_args()
    result = evaluate_promotion(
        manifest_path=Path(args.manifest),
        quality_report_path=Path(args.quality_report),
    )
    encoded = json.dumps(result, ensure_ascii=False, indent=2)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
