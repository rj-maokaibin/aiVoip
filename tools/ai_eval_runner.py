#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from pydantic import ValidationError

from app.diagnosis.ai_eval import (
    EvalGroundTruth,
    EvalObservation,
    EvalThresholds,
    build_model_quality_report,
    evaluate_observation,
)
from app.diagnosis.ai_proposal import AIProposal, _FORBIDDEN_COMMAND
from app.diagnosis.claim_grounding import ClaimGroundingValidator
from app.diagnosis.gateway import ReasoningGatewayClient


def _offline_validate(raw: dict, *, snapshot: dict, baseline: dict) -> tuple[dict | None, list[dict]]:
    """Offline validator used by Golden replay.

    Production Shadow evaluation additionally verifies Evidence ownership in the DB.
    Golden replay has no DB, so ownership is represented by the snapshot's Evidence
    IDs.  All other safety invariants remain fail-closed.
    """
    try:
        proposal = AIProposal.model_validate(raw)
    except ValidationError as exc:
        return None, [
            {
                "code": "SCHEMA_INVALID",
                "path": ".".join(str(x) for x in row["loc"]),
                "message": row["msg"],
            }
            for row in exc.errors(include_url=False)
        ]
    serialized = proposal.model_dump(mode="json")
    errors: list[dict] = []
    if _FORBIDDEN_COMMAND.search(str(serialized)):
        errors.append({"code": "COMMAND_OR_TEMPLATE_FORBIDDEN"})

    allowed_evidence_ids = {str(row.get("id")) for row in snapshot.get("evidences") or [] if row.get("id")}
    referenced = {
        str(evidence_id)
        for hypothesis in proposal.hypotheses
        for evidence_id in hypothesis.supporting_evidence_ids + hypothesis.contradicting_evidence_ids
    }
    referenced |= {
        ref.evidence_id
        for claim in proposal.claims
        for ref in claim.evidence
    }
    for evidence_id in sorted(referenced - allowed_evidence_ids):
        errors.append({"code": "EVIDENCE_NOT_IN_CASE", "evidence_id": evidence_id})

    if proposal.claims:
        grounding = ClaimGroundingValidator().validate(
            proposal.claims,
            allowed_evidence_ids=allowed_evidence_ids,
            ai_generated=True,
        )
        errors.extend(grounding.errors)

    baseline_excluded = set(baseline.get("excluded") or [])
    for claim in sorted(set(proposal.known) & baseline_excluded):
        errors.append({"code": "DETERMINISTIC_FACT_CONFLICT", "claim": claim})

    question_key = proposal.next_question_key
    action = proposal.recommended_action
    if action and action.action_type == "RECOMMEND_QUESTION":
        question_key = action.question_key or question_key
    if question_key:
        try:
            from app.reproduction.question_graph import DiagnosticQuestionRegistry
            DiagnosticQuestionRegistry().get(question_key)
        except Exception:
            errors.append({"code": "QUESTION_NOT_REGISTERED", "question_key": question_key})
    if action and action.action_type == "RECOMMEND_REPRODUCTION_PROFILE":
        try:
            from app.reproduction.profile import ReproductionProfileRegistry
            ReproductionProfileRegistry().get(action.profile_id or "")
        except Exception:
            errors.append({"code": "REPRODUCTION_PROFILE_NOT_REGISTERED", "profile_id": action.profile_id})
    if action and action.action_type == "RECOMMEND_EXPERIMENT_PROFILE":
        try:
            from app.experiments.profile import ExperimentProfileRegistry
            ExperimentProfileRegistry().get(action.experiment_profile_id or "")
        except Exception:
            errors.append({
                "code": "EXPERIMENT_PROFILE_NOT_REGISTERED",
                "experiment_profile_id": action.experiment_profile_id,
            })

    if errors:
        return None, errors
    for hypothesis in serialized["hypotheses"]:
        hypothesis["confidence"] = min(0.75, float(hypothesis["confidence"]))
        hypothesis["status"] = "OPEN"
        hypothesis["confirmable"] = False
        hypothesis["evidence_level"] = "L5"
    for claim in serialized.get("claims") or []:
        claim["status"] = "PROPOSED"
        claim["evidence_level"] = "L5"
    return serialized, []


def _run_case(row: dict, *, mode: str, gateway: ReasoningGatewayClient | None) -> tuple[EvalGroundTruth, EvalObservation]:
    ground_truth = EvalGroundTruth.model_validate(row["ground_truth"])
    snapshot = row.get("snapshot") or {}
    baseline = row.get("deterministic_baseline") or {}
    started = time.monotonic()
    raw = None
    gateway_model = None
    gateway_error = None

    if mode == "fixture":
        raw = row.get("proposal_fixture")
    else:
        gateway = gateway or ReasoningGatewayClient()
        gateway_model = gateway.model or None
        try:
            if not gateway.enabled():
                raise RuntimeError("REASONING_GATEWAY_DISABLED")
            response = gateway.enhance(snapshot, baseline)
            raw = response.get("proposal") if isinstance(response.get("proposal"), dict) else response
        except Exception as exc:
            gateway_error = f"{type(exc).__name__}:{exc}"

    latency_ms = int((time.monotonic() - started) * 1000)
    if gateway_error:
        observation = EvalObservation(
            case_id=ground_truth.case_id,
            proposal=None,
            validation_status="DEGRADED",
            validation_errors=[{"code": "GATEWAY_ERROR", "message": gateway_error}],
            latency_ms=latency_ms,
            gateway_model=gateway_model,
        )
        return ground_truth, observation

    if not isinstance(raw, dict):
        observation = EvalObservation(
            case_id=ground_truth.case_id,
            proposal=None,
            validation_status="REJECTED",
            validation_errors=[{"code": "MODEL_OUTPUT_NOT_OBJECT"}],
            latency_ms=latency_ms,
            gateway_model=gateway_model,
        )
        return ground_truth, observation

    validated, errors = _offline_validate(raw, snapshot=snapshot, baseline=baseline)
    observation = EvalObservation(
        case_id=ground_truth.case_id,
        proposal=validated or raw,
        validation_status="ACCEPTED" if validated is not None else "REJECTED",
        validation_errors=errors,
        latency_ms=latency_ms,
        estimated_cost_usd=row.get("estimated_cost_usd"),
        gateway_model=gateway_model,
    )
    return ground_truth, observation


def run_dataset(dataset: dict, *, mode: str = "fixture") -> dict:
    if dataset.get("schema_version") != "ai-model-eval-dataset-v2":
        raise ValueError("AI_MODEL_EVAL_DATASET_SCHEMA_INVALID")
    gateway = ReasoningGatewayClient() if mode == "gateway" else None
    evaluations = []
    observations = []
    for row in dataset.get("cases") or []:
        ground_truth, observation = _run_case(row, mode=mode, gateway=gateway)
        observations.append(observation.model_dump(mode="json"))
        evaluations.append(evaluate_observation(ground_truth, observation))

    thresholds_raw = dataset.get("thresholds") or {}
    thresholds = EvalThresholds(**thresholds_raw) if thresholds_raw else EvalThresholds()
    report = build_model_quality_report(
        evaluations,
        audit_events=dataset.get("audit_events") or [],
        audit_coverage_complete=bool(dataset.get("audit_coverage_complete")),
        thresholds=thresholds,
    )
    return {
        "schema_version": "ai-model-eval-run-v2",
        "dataset_id": dataset.get("dataset_id"),
        "mode": mode,
        "report": report,
        "evaluations": evaluations,
        "observations": observations,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run evidence-grounded AI model evaluation")
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--mode", choices=["fixture", "gateway"], default="fixture")
    parser.add_argument("--out", default="validation/ai_model_eval.json")
    parser.add_argument("--require-pass", action="store_true")
    args = parser.parse_args()

    dataset = json.loads(Path(args.dataset).read_text(encoding="utf-8"))
    result = run_dataset(dataset, mode=args.mode)
    encoded = json.dumps(result, ensure_ascii=False, indent=2)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    if args.require_pass and result["report"]["status"] != "PASS":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
