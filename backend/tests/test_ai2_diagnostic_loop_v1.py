from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.db.ai_intelligence_models import AIDiagnosticCycle
from app.db.base import Base
from app.db.models import AIProposalRecord, Case
from app.diagnosis.ai_cycle import AIDiagnosticCycleError, AIDiagnosticCycleService
from app.diagnosis.ai_runtime import AIPromotionStage, AIRuntimePolicy


class FakeSnapshotBuilder:
    def __init__(self, snapshot: dict):
        self.snapshot = snapshot

    def build_for_reasoning(self, db, case_id):
        payload = dict(self.snapshot)
        payload["case"] = dict(payload.get("case") or {})
        payload["case"]["id"] = case_id
        return payload


class FakeGateway:
    model = "ai2-test"

    def __init__(self, proposal: dict | None = None):
        self.proposal = proposal or _proposal()
        self.calls = 0

    def enabled(self):
        return True

    def enhance(self, snapshot, deterministic_baseline):
        self.calls += 1
        return {"proposal": self.proposal}


def _proposal(*, question_key: str = "AUDIO_NOISE_FAULT_LAYER") -> dict:
    return {
        "schema_version": "ai-proposal-v2",
        "intent": "DIAGNOSIS_ENHANCEMENT",
        "hypotheses": [
            {
                "code": "H_LOCAL_AUDIO",
                "title": "本地接收侧音频路径异常候选",
                "fault_domain": "LOCAL_AUDIO",
                "confidence": 0.70,
                "rationale": "当前 Case 仍需区分话机负载与本地模拟前端。",
                "supporting_evidence_ids": [],
                "contradicting_evidence_ids": [],
                "missing_evidence": ["HANDSET_AB_COMPARE"],
            }
        ],
        "claims": [],
        "known": ["当前证据已进入确定性分析"],
        "unknown": ["话机负载与本地模拟前端尚未区分"],
        "excluded": [],
        "next_question_key": question_key,
        "recommended_action": {
            "action_type": "RECOMMEND_QUESTION",
            "reason": "优先选择能区分当前候选的已注册问题。",
            "question_key": question_key,
            "profile_id": None,
            "experiment_profile_id": None,
        },
        "user_explanation": "该输出只作为候选和下一步建议，不改变正式诊断。",
    }


def _snapshot(*, fp: str = "a", evidence_fp: str = "e", status: str = "ANALYZING", sufficient: bool = False) -> dict:
    return {
        "schema_version": "case-intelligence-snapshot-v1",
        "case": {"id": "placeholder", "case_no": "CASE-AI2", "summary": "周期性电流音", "status": status},
        "devices": [],
        "evidences": [],
        "analyzers": {},
        "preliminary_reports": [],
        "diagnoses": [
            {
                "id": "diag-1",
                "status": "ANALYZING",
                "cycle": 1,
                "summary": {"known": ["RTP层未见明显异常"]},
                "decision": {
                    "known": ["RTP层未见明显异常"],
                    "unknown": ["本地模拟链路来源待区分"],
                    "excluded": [],
                },
            }
        ],
        "hypotheses": [
            {
                "id": "h1",
                "code": "H_LOCAL_AUDIO",
                "title": "本地音频路径候选",
                "fault_domain": "LOCAL_AUDIO",
                "status": "OPEN",
                "confidence": 0.6,
                "rationale": "deterministic baseline",
                "confirmable": False,
                "evidence": [],
            }
        ],
        "reproductions": [
            {"id": "r1", "evidence_sufficiency": "SUFFICIENT" if sufficient else "INSUFFICIENT"}
        ],
        "experiments": [],
        "fix_verifications": [],
        "authority": {"ai_can_confirm_root_cause": False},
        "source_evidence_fingerprint": (evidence_fp * 64)[:64],
        "snapshot_fingerprint": (fp * 64)[:64],
        "fingerprint": (fp * 64)[:64],
    }


def _runtime(stage: AIPromotionStage) -> AIRuntimePolicy:
    return AIRuntimePolicy(stage=stage, promotion_gate_passed=False, gate_source="NONE")


def _db():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return Session(engine)


def _case(db: Session, *, status: str = "ANALYZING") -> Case:
    row = Case(case_no="CASE-AI2", summary="周期性电流音", status=status)
    db.add(row)
    db.commit()
    return row


@pytest.fixture(autouse=True)
def _enable_ai2(monkeypatch):
    monkeypatch.setattr(settings, "ai_diagnostic_loop_enabled", True)
    monkeypatch.setattr(settings, "diagnosis_no_progress_limit", 2)
    monkeypatch.setattr(settings, "diagnosis_max_cycles", 6)


def test_shadow_records_hypothesis_and_critic_but_never_plans_or_dispatches():
    with _db() as db:
        case = _case(db)
        gateway = FakeGateway()
        result = AIDiagnosticCycleService(
            snapshot_builder=FakeSnapshotBuilder(_snapshot(fp="a")),
            gateway=gateway,
            runtime=_runtime(AIPromotionStage.SHADOW),
        ).run_next(db, case_id=case.id)

        row = result.row
        assert row.runtime_stage == "SHADOW"
        assert row.status == "COMPLETED"
        assert row.hypotheses_json[0]["status"] == "OPEN"
        assert row.hypotheses_json[0]["evidence_level"] == "L5"
        assert row.critic_json["status"] in {"PASS", "REVIEW"}
        assert row.next_action_json == {}
        assert row.selection_json == {}
        assert row.formal_result_changed is False
        assert row.dispatch_attempted is False
        assert row.dispatch_allowed is False
        assert gateway.calls == 1

        proposal = db.get(AIProposalRecord, row.proposal_id)
        assert proposal is not None
        assert proposal.mode == "SHADOW"
        assert proposal.diff_json["formal_result_changed"] is False


def test_suggest_returns_registered_recommendation_without_dispatch_authority():
    with _db() as db:
        case = _case(db)
        result = AIDiagnosticCycleService(
            snapshot_builder=FakeSnapshotBuilder(_snapshot(fp="b")),
            gateway=FakeGateway(),
            runtime=_runtime(AIPromotionStage.SUGGEST),
        ).run_next(db, case_id=case.id)

        row = result.row
        assert row.runtime_stage == "SUGGEST"
        assert row.status == "COMPLETED"
        assert row.next_action_json["type"] == "QUESTION"
        assert row.next_action_json["registered_id"] == "AUDIO_NOISE_FAULT_LAYER"
        assert row.next_action_json["dispatch_allowed"] is False
        assert row.next_action_json["raw_command_allowed"] is False
        assert row.selection_json["registered_id"] == "AUDIO_NOISE_FAULT_LAYER"
        assert row.selection_json["dispatch_allowed"] is False
        assert row.formal_result_changed is False
        assert row.dispatch_attempted is False
        assert row.dispatch_allowed is False


def test_same_snapshot_and_stage_is_idempotent_and_does_not_call_gateway_twice():
    with _db() as db:
        case = _case(db)
        gateway = FakeGateway()
        service = AIDiagnosticCycleService(
            snapshot_builder=FakeSnapshotBuilder(_snapshot(fp="c")),
            gateway=gateway,
            runtime=_runtime(AIPromotionStage.SHADOW),
        )
        first = service.run_next(db, case_id=case.id)
        second = service.run_next(db, case_id=case.id)
        assert first.row.id == second.row.id
        assert second.idempotent_replay is True
        assert gateway.calls == 1


def test_no_progress_stops_at_configured_limit_without_third_model_call():
    with _db() as db:
        case = _case(db)
        gateway = FakeGateway()
        for fp in ("d", "f"):
            result = AIDiagnosticCycleService(
                snapshot_builder=FakeSnapshotBuilder(_snapshot(fp=fp, evidence_fp="n")),
                gateway=gateway,
                runtime=_runtime(AIPromotionStage.SHADOW),
            ).run_next(db, case_id=case.id)
            assert result.row.continue_recommendation == "CONTINUE"
        third = AIDiagnosticCycleService(
            snapshot_builder=FakeSnapshotBuilder(_snapshot(fp="g", evidence_fp="n")),
            gateway=gateway,
            runtime=_runtime(AIPromotionStage.SHADOW),
        ).run_next(db, case_id=case.id)
        assert third.row.status == "STOPPED"
        assert third.row.stop_reason == "NO_PROGRESS_LIMIT"
        assert third.row.no_progress_count == 2
        assert third.row.proposal_id is None
        assert gateway.calls == 2


def test_evidence_sufficient_stops_before_model():
    with _db() as db:
        case = _case(db)
        gateway = FakeGateway()
        row = AIDiagnosticCycleService(
            snapshot_builder=FakeSnapshotBuilder(_snapshot(fp="h", sufficient=True)),
            gateway=gateway,
            runtime=_runtime(AIPromotionStage.SHADOW),
        ).run_next(db, case_id=case.id).row
        assert row.status == "STOPPED"
        assert row.stop_reason == "EVIDENCE_SUFFICIENT"
        assert row.continue_recommendation == "STOP"
        assert gateway.calls == 0


def test_formally_confirmed_root_cause_stops_before_model():
    with _db() as db:
        case = _case(db, status="ROOT_CAUSE_CONFIRMED")
        gateway = FakeGateway()
        row = AIDiagnosticCycleService(
            snapshot_builder=FakeSnapshotBuilder(_snapshot(fp="i", status="ROOT_CAUSE_CONFIRMED")),
            gateway=gateway,
            runtime=_runtime(AIPromotionStage.SHADOW),
        ).run_next(db, case_id=case.id).row
        assert row.status == "STOPPED"
        assert row.stop_reason == "ROOT_CAUSE_OR_CASE_TERMINAL"
        assert row.continue_recommendation == "STOP"
        assert gateway.calls == 0


def test_max_cycle_stops_and_never_changes_formal_result(monkeypatch):
    monkeypatch.setattr(settings, "diagnosis_max_cycles", 1)
    with _db() as db:
        case = _case(db)
        row = AIDiagnosticCycleService(
            snapshot_builder=FakeSnapshotBuilder(_snapshot(fp="j")),
            gateway=FakeGateway(),
            runtime=_runtime(AIPromotionStage.SHADOW),
        ).run_next(db, case_id=case.id).row
        assert row.status == "STOPPED"
        assert row.stop_reason == "MAX_CYCLES_REACHED"
        assert row.formal_result_changed is False
        assert row.dispatch_attempted is False


def test_controlled_planner_is_not_opened_by_ai2_v1_software_gate():
    with _db() as db:
        case = _case(db)
        with pytest.raises(AIDiagnosticCycleError, match="AI2_CONTROLLED_PLANNER_NOT_ENABLED_BY_V1_GATE"):
            AIDiagnosticCycleService(
                snapshot_builder=FakeSnapshotBuilder(_snapshot(fp="k")),
                gateway=FakeGateway(),
                runtime=_runtime(AIPromotionStage.CONTROLLED_PLANNER),
            ).run_next(db, case_id=case.id)
        assert db.scalar(select(AIDiagnosticCycle).where(AIDiagnosticCycle.case_id == case.id)) is None


def test_unregistered_model_recommendation_fails_closed_without_dispatch():
    with _db() as db:
        case = _case(db)
        row = AIDiagnosticCycleService(
            snapshot_builder=FakeSnapshotBuilder(_snapshot(fp="m")),
            gateway=FakeGateway(_proposal(question_key="NOT_REGISTERED_BY_AI")),
            runtime=_runtime(AIPromotionStage.SUGGEST),
        ).run_next(db, case_id=case.id).row
        assert row.status == "DEGRADED"
        assert row.continue_recommendation == "REQUIRE_HUMAN"
        assert row.stop_reason == "AI_PROPOSAL_UNAVAILABLE"
        assert row.dispatch_attempted is False
        assert row.dispatch_allowed is False
