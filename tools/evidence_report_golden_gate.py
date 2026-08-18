#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.contracts.evidence_report import P0_FINDING_TYPES  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.reports.finding_composer import compose_findings, derive_first_observable_layer  # noqa: E402


FORBIDDEN_INPUT_KEYS = {"expected", "ground_truth", "answer", "expected_findings", "expected_boundary"}
SERIOUS_SEVERITIES = {"HIGH", "CRITICAL"}


def _contains_forbidden(value) -> bool:
    if isinstance(value, dict):
        if any(str(k).lower() in FORBIDDEN_INPUT_KEYS for k in value):
            return True
        return any(_contains_forbidden(v) for v in value.values())
    if isinstance(value, list):
        return any(_contains_forbidden(v) for v in value)
    return False


def _safe_div(a: int, b: int) -> float:
    return 1.0 if b == 0 else a / b


def evaluate(dataset: dict) -> dict:
    rows = dataset.get("cases") or []
    if not rows:
        raise ValueError("GOLDEN_DATASET_EMPTY")

    labelled_types = Counter(
        str(ftype)
        for row in rows
        for ftype in ((row.get("expected") or {}).get("finding_types") or [])
    )
    missing_p0_types = sorted(P0_FINDING_TYPES - set(labelled_types))

    by_type = defaultdict(lambda: Counter(tp=0, fp=0, fn=0))
    totals = Counter(tp=0, fp=0, fn=0)
    boundary_total = boundary_correct = wrong_boundary = unknown_expected = unknown_correct = 0
    leakage = []
    serious_false_positives: list[dict] = []
    case_results = []

    for row in rows:
        case_id = str(row.get("id") or "")
        input_payload = row.get("input") or {}
        expected = row.get("expected") or {}
        if _contains_forbidden(input_payload):
            leakage.append(case_id)
            continue

        findings = compose_findings(
            packet=input_payload.get("packet"),
            pcm=input_payload.get("pcm"),
            media=input_payload.get("media"),
            source_run_ids={},
        )
        actual_types = Counter(str(f.get("type")) for f in findings)
        expected_types = Counter(str(x) for x in expected.get("finding_types", []))
        for ftype in sorted(set(actual_types) | set(expected_types)):
            tp = min(actual_types[ftype], expected_types[ftype])
            fp = max(0, actual_types[ftype] - expected_types[ftype])
            fn = max(0, expected_types[ftype] - actual_types[ftype])
            by_type[ftype].update(tp=tp, fp=fp, fn=fn)
            totals.update(tp=tp, fp=fp, fn=fn)
        remaining_expected = Counter(expected_types)
        for finding in findings:
            ftype = str(finding.get("type"))
            if remaining_expected[ftype] > 0:
                remaining_expected[ftype] -= 1
                continue
            if str(finding.get("severity") or "INFO").upper() in SERIOUS_SEVERITIES:
                serious_false_positives.append({"case_id": case_id, "type": ftype, "severity": finding.get("severity")})

        boundary_actual = derive_first_observable_layer(input_payload.get("layer_observations") or [])
        boundary_expected = expected.get("boundary")
        boundary_ok = None
        if boundary_expected is not None:
            boundary_total += 1
            exp_status = str(boundary_expected.get("status"))
            if exp_status == "UNKNOWN":
                unknown_expected += 1
                boundary_ok = boundary_actual.get("status") == "UNKNOWN"
                if boundary_ok:
                    unknown_correct += 1
                elif boundary_actual.get("status") == "OBSERVED_BOUNDARY":
                    wrong_boundary += 1
            else:
                boundary_ok = (
                    boundary_actual.get("status") == exp_status
                    and boundary_actual.get("first_observable_layer") == boundary_expected.get("first_observable_layer")
                )
                if not boundary_ok and boundary_actual.get("status") == "OBSERVED_BOUNDARY":
                    wrong_boundary += 1
            if boundary_ok:
                boundary_correct += 1

        case_results.append({
            "id": case_id,
            "actual_finding_types": list(actual_types.elements()),
            "expected_finding_types": list(expected_types.elements()),
            "boundary_actual": boundary_actual,
            "boundary_expected": boundary_expected,
            "boundary_ok": boundary_ok,
        })

    recall = _safe_div(totals["tp"], totals["tp"] + totals["fn"])
    precision = _safe_div(totals["tp"], totals["tp"] + totals["fp"])
    boundary_correctness = _safe_div(boundary_correct, boundary_total)
    wrong_rate = _safe_div(wrong_boundary, boundary_total)
    per_type = {}
    for ftype, c in sorted(by_type.items()):
        per_type[ftype] = {
            **dict(c),
            "recall": round(_safe_div(c["tp"], c["tp"] + c["fn"]), 6),
            "precision": round(_safe_div(c["tp"], c["tp"] + c["fp"]), 6),
        }
    per_p0_type_pass = not missing_p0_types and all(
        ftype in per_type
        and per_type[ftype]["recall"] >= settings.evidence_report_golden_min_recall
        and per_type[ftype]["precision"] >= settings.evidence_report_golden_min_precision
        for ftype in P0_FINDING_TYPES
    )

    gates = {
        "no_answer_leakage": not leakage,
        "p0_dataset_coverage": not missing_p0_types,
        "recall": recall >= settings.evidence_report_golden_min_recall,
        "precision": precision >= settings.evidence_report_golden_min_precision,
        "per_p0_finding_type_recall_precision": per_p0_type_pass,
        "serious_false_positive_regression": not serious_false_positives,
        "boundary_correctness": boundary_correctness >= settings.evidence_report_boundary_min_correctness,
        "wrong_boundary_rate": wrong_rate < settings.evidence_report_boundary_max_wrong_rate,
        "unknown_safety": unknown_expected == unknown_correct,
    }
    return {
        "schema_version": "evidence-report-golden-gate-v1",
        "dataset_version": dataset.get("schema_version"),
        "case_count": len(rows),
        "evaluated_case_count": len(case_results),
        "status": "PASS" if all(gates.values()) else "FAIL",
        "gates": gates,
        "p0_required_types": sorted(P0_FINDING_TYPES),
        "p0_labelled_types": sorted(set(labelled_types) & P0_FINDING_TYPES),
        "p0_missing_types": missing_p0_types,
        "thresholds": {
            "min_recall": settings.evidence_report_golden_min_recall,
            "min_precision": settings.evidence_report_golden_min_precision,
            "min_boundary_correctness": settings.evidence_report_boundary_min_correctness,
            "max_wrong_boundary_rate": settings.evidence_report_boundary_max_wrong_rate,
            "serious_false_positive_allowed": 0,
        },
        "metrics": {
            "tp": totals["tp"], "fp": totals["fp"], "fn": totals["fn"],
            "recall": round(recall, 6), "precision": round(precision, 6),
            "p0_required_type_count": len(P0_FINDING_TYPES),
            "p0_covered_type_count": len(P0_FINDING_TYPES) - len(missing_p0_types),
            "boundary_total": boundary_total,
            "boundary_correctness": round(boundary_correctness, 6),
            "wrong_boundary_rate": round(wrong_rate, 6),
            "unknown_expected": unknown_expected,
            "unknown_correct": unknown_correct,
            "serious_false_positive_count": len(serious_false_positives),
        },
        "per_type": per_type,
        "serious_false_positives": serious_false_positives,
        "answer_leakage_cases": leakage,
        "cases": case_results,
        "environment_gate_note": "Synthetic/Lab/Field composition is required for production release; real dataset acceptance is intentionally external to this software-only gate.",
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", type=Path, default=ROOT / "golden_cases" / "evidence_report_v1_synthetic.json")
    ap.add_argument("--out", type=Path, default=ROOT / "validation" / "evidence_report_golden_gate.json")
    args = ap.parse_args()
    payload = json.loads(args.dataset.read_text(encoding="utf-8"))
    result = evaluate(payload)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "metrics": result["metrics"], "p0_missing_types": result["p0_missing_types"], "out": str(args.out)}, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
