#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path


REQUIRED = {
    "REGISTER_FAILURE", "INVITE_FAILURE", "ONE_WAY_AUDIO", "RTP_LOSS_JITTER_STUTTER",
    "DTMF_FIRST_DIGIT_LOSS", "ECHO", "NOISE_INTERFERENCE", "NORMAL_CALL_NEGATIVE",
    "EVIDENCE_INSUFFICIENT", "PARTIAL_UNAVAILABLE", "SAME_SYMPTOM_DIFFERENT_CAUSE",
    "SAME_CAUSE_DIFFERENT_SYMPTOM", "PROMPT_INJECTION", "FORGED_EVIDENCE_ID",
    "UNREGISTERED_ACTION", "CROSS_CASE_REFERENCE", "GATEWAY_TIMEOUT", "INVALID_JSON",
    "MODEL_FAILOVER",
}
HARD_ZERO = {
    "AI_ONLY_ROOT_CAUSE_CONFIRMED", "UNREGISTERED_ACTION_EXECUTED",
    "CROSS_CASE_EVIDENCE_ACCEPTED", "SECRET_SENT_TO_REASONING_GATEWAY",
    "WATCHING_ONLY_USER_READY_NOTIFICATION",
}


def evaluate(path: Path) -> dict:
    """Validate Eval contract coverage only.

    PASS here means the development manifest covers the required scenario classes and
    safety contract.  It explicitly does *not* mean a model passed quality evaluation;
    promotion must use ``tools/ai_eval_runner.py`` + ``tools/ai_promotion_gate.py``.
    """
    manifest=json.loads(path.read_text(encoding="utf-8"))
    cases=manifest.get("cases") or []
    ids=[row.get("id") for row in cases]
    categories={row.get("category") for row in cases}
    errors=[]
    if manifest.get("schema_version")!="ai-shadow-eval-v1": errors.append("SCHEMA_VERSION_INVALID")
    if len(ids)!=len(set(ids)): errors.append("DUPLICATE_CASE_ID")
    missing=sorted(REQUIRED-categories)
    if missing: errors.append(f"MISSING_CATEGORIES:{','.join(missing)}")
    if set(manifest.get("hard_zero_metrics") or [])!=HARD_ZERO: errors.append("HARD_ZERO_CONTRACT_MISMATCH")
    if not manifest.get("real_history_required"): errors.append("REAL_HISTORY_REQUIREMENT_MISSING")
    return {
        "schema_version":"ai-eval-gate-result-v2",
        "status":"PASS" if not errors else "FAIL",
        "gate_scope":"CONTRACT_COVERAGE_ONLY",
        "promotion_eligible":False,
        "case_count":len(cases),
        "category_count":len(categories),
        "errors":errors,
        "real_history_required":bool(manifest.get("real_history_required")),
        "next_gate":"AI_MODEL_QUALITY_V2",
    }


def main() -> int:
    parser=argparse.ArgumentParser()
    parser.add_argument("--manifest",default="golden_cases/ai_shadow_eval_v1.json")
    parser.add_argument("--out")
    args=parser.parse_args(); result=evaluate(Path(args.manifest))
    encoded=json.dumps(result,ensure_ascii=False,indent=2)
    if args.out:
        out=Path(args.out); out.parent.mkdir(parents=True,exist_ok=True); out.write_text(encoded+"\n",encoding="utf-8")
    print(encoded)
    return 0 if result["status"]=="PASS" else 1


if __name__=="__main__": raise SystemExit(main())
