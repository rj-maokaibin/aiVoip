from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.models import AIProposalRecord, AIRecommendationFeedback, Case, Evidence
from app.diagnosis.ai_workbench import (
    EngineeringDraftRequest, build_eval_report, build_readonly_workbench,
    persist_engineering_draft, persist_readonly_workbench,
)


def _snapshot(case_id: str, evidence_id: str = "e1") -> dict:
    return {
        "fingerprint": "f" * 64,
        "case": {"id": case_id, "case_no": "AI-W-1", "summary": "通话电流音", "status": "ANALYZING"},
        "devices": [],
        "evidences": [{
            "id": evidence_id, "type": "PCAP", "filename": "call.pcap", "sha256": "a" * 64,
            "completeness": "COMPLETE", "metadata": {},
        }],
        "analyzers": {"packet_intelligence": {
            "run_id": "run1", "status": "SUCCESS", "version": "1.0",
            "input_evidence_ids": [evidence_id], "summary": {}, "result": {},
        }},
    }


def _baseline() -> dict:
    return {
        "hypotheses": [{
            "code": "DET_NOISE", "title": "媒体路径噪声候选", "fault_domain": "AUDIO",
            "confidence": 0.7, "status": "OPEN",
            "evidence": [{"ref_type": "ANALYZER_RUN", "ref_id": "run1", "level": "L1"}],
        }],
        "known": ["抓包完整"], "unknown": ["噪声首次出现层"], "excluded": ["电源故障已排除"],
        "summary": {"headline": "媒体路径噪声候选"},
    }


def test_readonly_workbench_covers_quality_critic_planning_and_five_roles():
    proposal = {
        "known": ["电源故障已排除"],
        "hypotheses": [{
            "code": "AI_POWER", "title": "电源候选", "fault_domain": "POWER",
            "supporting_evidence_ids": [], "missing_evidence": ["电源替换 A/B"],
        }],
    }
    result = build_readonly_workbench(_snapshot("c"), _baseline(), proposal)
    assert result["formal_result_changed"] is False
    assert result["evidence_quality"]["status"] == "PASS"
    assert result["critic"]["status"] == "REJECT"
    assert result["critic"]["hard_contradictions"] == ["电源故障已排除"]
    assert result["planning"]["profile_recommendation"]["auto_create_session"] is False
    assert result["planning"]["question_recommendation"]["question_key"]
    assert set(result["role_explanations"]) == {"FIELD", "SUPPORT", "ENGINEERING", "CUSTOMER", "TEACHING"}
    assert all(row["evidence_ids"] == ["run1"] for row in result["role_explanations"].values())


def test_quality_auditor_flags_partial_unanalyzed_evidence():
    snapshot = _snapshot("c")
    snapshot["evidences"][0]["completeness"] = "PARTIAL"
    snapshot["analyzers"] = {}
    result = build_readonly_workbench(snapshot, _baseline())
    codes = {issue["code"] for issue in result["evidence_quality"]["issues"]}
    assert {"EVIDENCE_NOT_COMPLETE", "ANALYZER_NOT_RUN", "CONCLUSION_REFERENCE_UNAVAILABLE"} <= codes
    assert result["evidence_quality"]["status"] == "BLOCKED"


def test_readonly_record_is_idempotent_and_engineering_output_stays_draft():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        case = Case(case_no="AI-W-2", summary="电流音", status="ANALYZING")
        db.add(case); db.flush()
        evidence = Evidence(case_id=case.id, type="PCAP", source="UPLOAD", filename="x.pcap",
                            object_key="x", size_bytes=1, sha256="a" * 64)
        db.add(evidence); db.flush()
        snapshot = _snapshot(case.id, evidence.id)
        first = persist_readonly_workbench(db, case_id=case.id, diagnosis_run_id=None,
                                           snapshot=snapshot, baseline=_baseline())
        second = persist_readonly_workbench(db, case_id=case.id, diagnosis_run_id=None,
                                            snapshot=snapshot, baseline=_baseline())
        assert first.id == second.id
        draft = persist_engineering_draft(
            db, case_id=case.id,
            request=EngineeringDraftRequest(draft_type="RULE", objective="覆盖噪声反例"),
            snapshot=snapshot, baseline=_baseline(), actor="tester",
        )
        assert draft.mode == "DRAFT" and draft.status == "DRAFT"
        assert draft.validated_output_json["proposal"]["publishable"] is False
        assert draft.validated_output_json["proposal"]["executable"] is False
        assert len(list(db.scalars(select(AIProposalRecord)))) == 2


def test_eval_gate_reports_hard_zero_contract_and_requires_samples(monkeypatch):
    monkeypatch.setattr("app.diagnosis.ai_workbench.settings.ai_eval_min_samples", 1)
    row = AIProposalRecord(
        case_id="c", schema_version="ai-proposal-v1", intent="DIAGNOSIS_ENHANCEMENT",
        mode="SHADOW", status="ACCEPTED", input_fingerprint="f" * 64,
        workflow_version="v1", latency_ms=12,
        validated_output_json={"hypotheses": [{"supporting_evidence_ids": ["e"]}]},
        validation_errors=[], baseline_json={},
        diff_json={"overlap_codes": ["H"], "ai_only_codes": [], "formal_result_changed": False},
    )
    feedback=AIRecommendationFeedback(proposal_id="p",case_id="c",item_type="PROFILE",
                                      decision="ACCEPTED",actor="tester")
    report = build_eval_report([row],[feedback])
    assert report["status"] == "PASS"
    assert report["metrics"]["evidence_reference_accuracy"] == 1.0
    assert report["metrics"]["question_profile_recommendation_acceptance"] == 1.0
    assert all(value == 0 for value in report["hard_zero_metrics"].values())
    assert report["gate"]["auto_action_enabled"] is False
