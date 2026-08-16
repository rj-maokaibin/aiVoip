from pathlib import Path

from app.diagnosis.ai_eval import (
    EvalGroundTruth,
    EvalObservation,
    EvalThresholds,
    build_model_quality_report,
    evaluate_observation,
    hard_zero_from_audit,
)
from app.diagnosis.ai_runtime import AICapability, AIPromotionStage, AIRuntimePolicy
from app.diagnosis.claim_grounding import ClaimGroundingValidator, DiagnosticClaim
from app.diagnosis.discriminating_planner import select_question
from app.diagnosis.gateway import compact_context
from app.diagnosis.path_reasoning import PathObservation, derive_first_mismatch_boundary
from app.knowledge.similarity import CaseSignature, CaseSimilarity
from tools.ai_eval_runner import run_dataset


def test_runtime_promotion_never_grants_formal_or_command_authority():
    policy = AIRuntimePolicy(AIPromotionStage.CONTROLLED_PLANNER, promotion_gate_passed=True)
    assert policy.enabled(AICapability.REGISTERED_PLAN_SELECTION) is True
    assert policy.formal_reasoner_may_use_ai is False
    assert policy.may_execute_device_command is False


def test_claim_grounding_rejects_cross_case_and_ai_self_promotion():
    claim = DiagnosticClaim(
        claim_id="C1",
        claim_type="CAUSE",
        statement="candidate cause",
        subject="media",
        predicate="CAUSE",
        value="x",
        status="SUPPORTED",
        evidence_level="L1",
        evidence=[{"evidence_id": "foreign", "relation": "SUPPORT"}],
    )
    result = ClaimGroundingValidator().validate(
        [claim], allowed_evidence_ids={"local"}, ai_generated=True
    )
    codes = {row["code"] for row in result.errors}
    assert "AI_CLAIM_EVIDENCE_LEVEL_INVALID" in codes
    assert "AI_CLAIM_SELF_PROMOTION_FORBIDDEN" in codes
    assert "CLAIM_EVIDENCE_NOT_IN_CASE" in codes
    assert result.status == "REJECT"


def test_dtmf_first_mismatch_boundary_is_evidence_grounded_l5():
    claim = derive_first_mismatch_boundary(
        path_name="DTMF_PATH",
        reference_value="123456",
        observations=[
            PathObservation("PCM_RX", "123456", "pcm-evidence", direction="RX"),
            PathObservation("AIM_GETNUMBER", "23456", "aim-log", direction="RX"),
            PathObservation("SIP_DTMF_FORWARD", "23456", "pcap", direction="TX"),
        ],
    )
    assert claim is not None
    assert claim.claim_type == "BOUNDARY"
    assert claim.value == "PCM_RX->AIM_GETNUMBER"
    assert claim.evidence_level == "L5"
    assert claim.status == "PROPOSED"
    assert {edge.evidence_id for edge in claim.evidence} == {"pcm-evidence", "aim-log"}


def test_model_quality_requires_real_verified_samples_and_complete_audit():
    truth = EvalGroundTruth(
        case_id="real-1",
        category="DTMF_FIRST_DIGIT_LOSS",
        source_kind="REAL",
        verification_status="FIX_VERIFIED",
        expected_hypothesis_codes=["DTMF_CPU_PATH"],
        expected_fault_domains=["DTMF"],
        allowed_evidence_ids=["e1"],
    )
    observation = EvalObservation(
        case_id="real-1",
        validation_status="ACCEPTED",
        proposal={
            "hypotheses": [{
                "code": "DTMF_CPU_PATH", "fault_domain": "DTMF", "confidence": 0.7,
                "supporting_evidence_ids": ["e1"],
            }],
            "claims": [],
        },
    )
    evaluated = evaluate_observation(truth, observation)
    thresholds = EvalThresholds(minimum_samples=1)
    incomplete = build_model_quality_report([evaluated], thresholds=thresholds)
    assert incomplete["status"] == "INSUFFICIENT_DATA"
    report = build_model_quality_report(
        [evaluated], thresholds=thresholds, audit_events=[], audit_coverage_complete=True
    )
    assert report["status"] == "PASS"
    assert report["gate"]["promotion_eligible"] is True


def test_hard_zero_is_derived_from_audit_events_not_constants():
    metrics = hard_zero_from_audit([
        {"event_type": "AI_ONLY_ROOT_CAUSE_CONFIRMED", "detail": {}},
        {"event_type": "OTHER", "detail": {"hard_zero_metric": "UNREGISTERED_ACTION_EXECUTED"}},
    ])
    assert metrics["AI_ONLY_ROOT_CAUSE_CONFIRMED"] == 1
    assert metrics["UNREGISTERED_ACTION_EXECUTED"] == 1
    assert metrics["SECRET_SENT_TO_REASONING_GATEWAY"] == 0


def test_fixture_eval_runner_evaluates_model_output_against_ground_truth():
    dataset = {
        "schema_version": "ai-model-eval-dataset-v2",
        "dataset_id": "test",
        "audit_coverage_complete": True,
        "thresholds": {"minimum_samples": 1},
        "audit_events": [],
        "cases": [{
            "ground_truth": {
                "case_id": "c1",
                "category": "DTMF_FIRST_DIGIT_LOSS",
                "source_kind": "REAL",
                "verification_status": "FIX_VERIFIED",
                "expected_hypothesis_codes": ["DTMF_CPU_PATH"],
                "expected_fault_domains": ["DTMF"],
                "allowed_evidence_ids": ["e1"],
            },
            "snapshot": {"evidences": [{"id": "e1"}]},
            "deterministic_baseline": {"excluded": []},
            "proposal_fixture": {
                "schema_version": "ai-proposal-v2",
                "intent": "DIAGNOSIS_ENHANCEMENT",
                "hypotheses": [{
                    "code": "DTMF_CPU_PATH",
                    "title": "DTMF path",
                    "fault_domain": "DTMF",
                    "confidence": 0.7,
                    "rationale": "e1 shows the first mismatch",
                    "supporting_evidence_ids": ["e1"],
                    "contradicting_evidence_ids": [],
                    "missing_evidence": [],
                }],
                "claims": [],
                "known": [], "unknown": [], "excluded": [],
                "next_question_key": None,
                "recommended_action": None,
                "user_explanation": "candidate only",
            },
        }],
    }
    result = run_dataset(dataset, mode="fixture")
    assert result["report"]["status"] == "PASS"
    assert result["report"]["metrics"]["top1_hypothesis_recall"] == 1.0


def test_discriminating_planner_picks_dtmf_path_question():
    snapshot = {"case": {"summary": "重启后首次拨号 DTMF 丢第一位"}, "analyzers": {}}
    baseline = {"hypotheses": [
        {"code": "DTMF_SLIC_PATH", "fault_domain": "DTMF", "title": "SLIC path", "confidence": 0.7},
        {"code": "DTMF_SIP_FORWARD", "fault_domain": "SIP", "title": "SIP forward", "confidence": 0.6},
    ]}
    planned = select_question(snapshot, baseline)
    assert planned.question_key == "DTMF_FIRST_MISMATCH_LAYER"
    assert set(planned.distinguishes) == {"DTMF_SLIC_PATH", "DTMF_SIP_FORWARD"}


def test_similarity_v2_explains_same_and_different_points():
    a = CaseSignature("a", "重启首次拨号丢第一位", {"DTMF_FIRST_DIGIT_LOSS"}, {"DTMF"}, {"r412"}, {"DTMF_LOSS"})
    b = CaseSignature("b", "设备重启后首次拨号少第一个号码", {"DTMF_FIRST_DIGIT_LOSS"}, {"DTMF"}, {"r412"}, {"DTMF_LOSS"})
    score, details = CaseSimilarity().score(a, b)
    assert score > 0.35
    assert details["algorithm_version"] == "2.0.0"
    assert details["same_points"]
    assert details["transferability"] in {"HIGH", "MEDIUM"}


def test_gateway_context_recursively_redacts_nested_secret_and_identifiers(monkeypatch):
    monkeypatch.setattr("app.diagnosis.gateway.settings.reasoning_gateway_include_device_identifiers", False)
    context = compact_context({
        "case": {"summary": "call 13800138000 from 192.168.1.10 password=abc", "status": "ANALYZING"},
        "devices": [{"id": "d1", "ip": "192.168.1.2", "sn": "SN", "platform_id": "p"}],
        "evidences": [],
        "analyzers": {"x": {"summary": {"token": "secret-token", "peer": "10.0.0.1"}, "result": {}}},
    })
    rendered = str(context)
    assert "13800138000" not in rendered
    assert "192.168.1.10" not in rendered
    assert "10.0.0.1" not in rendered
    assert "secret-token" not in rendered
    assert "[REDACTED" in rendered
