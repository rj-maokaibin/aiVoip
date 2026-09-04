#!/usr/bin/env python3
"""Emit backend test files not already executed by dedicated release-gate phases.

The Full Acceptance job runs several authoritative focused gates before the full
backend regression. Re-running those exact test files in the final regression
adds wall time without adding coverage. This planner subtracts only test files
that are guaranteed to have run earlier in the same exact-SHA job.

Fail-closed rules:
- unconditional dedicated groups must exist in full;
- conditional AI1/AI2/AI3 groups are excluded only when their sentinel exists;
- every excluded path must exist;
- at least one remaining backend test must be emitted.
"""
from __future__ import annotations

import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = ROOT / "backend" / "tests"

FROZEN = [
    "test_prd_spec_v1_alignment.py",
    "test_evidence_visuals_spec_v1.py",
    "test_evidence_report_interrupted_call_v1.py",
    "test_evidence_report_no_valid_call_v1.py",
    "test_evidence_bundle_frozen_v1.py",
    "test_evidence_report_web_drilldown_v1.py",
    "test_evidence_retention_download_v1.py",
    "test_evidence_audit_contract_v1.py",
    "test_feishu_case_card_update_contract_v1.py",
    "test_feishu_living_document_v1.py",
]

AI_E1_E6 = [
    "test_ai_eval_gate.py",
    "test_ai_proposal_shadow.py",
    "test_ai_readonly_workbench.py",
    "test_gateway_safety.py",
    "test_knowledge_similarity.py",
    "test_claim_grounding.py",
    "test_ai_e1_e6.py",
    "test_ai_promotion_runtime.py",
    "test_controlled_ai_selection.py",
    "test_golden_candidates.py",
    "test_golden_auto.py",
]

AI1 = [
    "test_ai1_semantic_router_v1.py",
    "test_ai1_semantic_gateway_v1.py",
    "test_ai1_semantic_eval_gate_v1.py",
    "test_ai1_semantic_api_v1.py",
    "test_ai1_semantic_real_corpus_eval_v1.py",
]

AI3 = [
    "test_ai3_case_copilot_v1.py",
    "test_ai3_copilot_gateway_v1.py",
    "test_ai3_copilot_api_v1.py",
    "test_ai3_copilot_idempotency_isolation_v1.py",
    "test_ai3_feishu_copilot_v1.py",
    "test_ai3_feishu_tenant_idempotency_v1.py",
    "test_ai3_copilot_fail_closed_v1.py",
]

AI2 = [
    "test_ai2_diagnostic_loop_v1.py",
    "test_ai2_cycles_api_v1.py",
    "test_ai2_diagnosis_sidecar_v1.py",
    "test_ai2_cycle_concurrency_contract_v1.py",
    "test_ai2_reasoning_gateway_redaction_v1.py",
    "test_ai2_suggest_bridge_v1.py",
    "test_ai2_suggest_concurrency_contract_v1.py",
    "test_ai2_reproduction_publish_recovery_v1.py",
    "test_ai2_feishu_suggest_v1.py",
    "test_ai2_feishu_retry_card_v1.py",
    "test_ai2_feishu_dispatch_order_v1.py",
]

M7 = ["test_m7_acceptance_gate.py"]


def _paths(names: list[str]) -> list[Path]:
    return [TEST_ROOT / name for name in names]


def _require_group(name: str, names: list[str]) -> list[Path]:
    paths = _paths(names)
    missing = [p for p in paths if not p.is_file()]
    if missing:
        rendered = ", ".join(str(p.relative_to(ROOT)) for p in missing)
        raise SystemExit(f"dedup contract failed: required {name} test(s) missing: {rendered}")
    return paths


def _conditional_group(name: str, names: list[str]) -> list[Path]:
    sentinel = TEST_ROOT / names[0]
    if not sentinel.is_file():
        return []
    return _require_group(name, names)


def build_plan() -> tuple[list[Path], list[Path]]:
    all_tests = sorted(TEST_ROOT.rglob("test_*.py"))
    if not all_tests:
        raise SystemExit("dedup contract failed: no backend tests discovered")

    dedicated: list[Path] = []
    dedicated += _require_group("frozen", FROZEN)
    dedicated += _require_group("ai_e1_e6", AI_E1_E6)
    dedicated += _conditional_group("ai1", AI1)
    dedicated += _conditional_group("ai3", AI3)
    dedicated += _conditional_group("ai2", AI2)
    dedicated += _require_group("m7", M7)

    dedicated_set = set(dedicated)
    remaining = [p for p in all_tests if p not in dedicated_set]
    if not remaining:
        raise SystemExit("dedup contract failed: no remaining backend regression tests")
    return dedicated, remaining


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, help="write remaining test paths, one per line")
    args = parser.parse_args()

    dedicated, remaining = build_plan()
    lines = [str(p.relative_to(ROOT)) for p in remaining]
    payload = "\n".join(lines) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(payload, encoding="utf-8")
    else:
        print(payload, end="")

    print(
        "BACKEND_REGRESSION_DEDUP_PLAN=PASS "
        f"dedicated_files={len(dedicated)} remaining_files={len(remaining)} total_files={len(dedicated) + len(remaining)}",
        file=__import__("sys").stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
