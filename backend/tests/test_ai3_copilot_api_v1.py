from __future__ import annotations

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.v1 import ai_copilot as api
from app.auth.providers import AuthIdentity
from app.contracts.enums import UserRole
from app.copilot.service import CopilotResult
from app.core.config import settings
from app.db.ai_intelligence_models import AICaseCopilotRecord
from app.db.base import Base
from app.db.models import Case


def _db():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return Session(engine)


def _identity(role=UserRole.VIEWER):
    return AuthIdentity("actor-ai3", role, True, "test")


def _case(db):
    row = Case(case_no="CASE-AI3-API", summary="周期性电流音", status="ANALYZING")
    db.add(row)
    db.commit()
    return row


def test_copilot_api_is_runtime_gated(monkeypatch):
    monkeypatch.setattr(settings, "ai_case_copilot_enabled", False)
    with _db() as db:
        case = _case(db)
        try:
            api.ask_case_copilot(
                case.id,
                api.CaseCopilotRequest(question="现在证据说明什么？", request_id="req-disabled"),
                db=db,
                identity=_identity(),
            )
        except Exception as exc:
            assert getattr(exc, "status_code", None) == 503
        else:
            raise AssertionError("disabled AI3 API must fail closed")


def test_copilot_api_returns_only_grounded_answer_and_authority_boundary(monkeypatch):
    monkeypatch.setattr(settings, "ai_case_copilot_enabled", True)
    seen = {}

    class _Service:
        def answer(self, db_arg, **kwargs):
            seen.update(kwargs)
            return CopilotResult(
                status="ANSWERED",
                answer="当前 Case 的 RTP 证据显示异常，但根因尚未确认。",
                proposal={
                    "schema_version": "ai-case-copilot-v1",
                    "answer": "当前 Case 的 RTP 证据显示异常，但根因尚未确认。",
                    "claims": [{"claim_id": "c1", "evidence": [{"evidence_id": "ev-1"}]}],
                    "cited_evidence_ids": ["ev-1"],
                    "uncertainty": ["根因尚未确认"],
                    "next_steps": [],
                    "root_cause_confirmed_by_ai": False,
                    "safety_class": "READ_ONLY_GROUNDED_RESPONSE",
                },
                grounding={"status": "PASS"},
                record_id="copilot-1",
            )

    monkeypatch.setattr(api, "CaseCopilotService", _Service)
    with _db() as db:
        case = _case(db)
        identity = _identity(UserRole.VIEWER)
        result = api.ask_case_copilot(
            case.id,
            api.CaseCopilotRequest(question="现在证据说明什么？", request_id="req-answer"),
            db=db,
            identity=identity,
        )
        assert result["status"] == "ANSWERED"
        assert result["read_only"] is True
        assert result["root_cause_authority"] == "DETERMINISTIC_OR_HUMAN_CONFIRMED_ONLY"
        assert result["execution_authority"] == "DETERMINISTIC_ROUTER_RBAC_POLICY_ORCHESTRATOR"
        assert result["proposal"]["root_cause_confirmed_by_ai"] is False
        assert seen["actor_role"] == UserRole.VIEWER
        assert seen["request_key"] == api._api_request_key(
            case_id=case.id,
            request_id="req-answer",
            identity=identity,
        )
        assert seen["request_key"].startswith("api:")
        assert "req-answer" not in seen["request_key"]


def test_copilot_api_control_result_is_non_executing(monkeypatch):
    monkeypatch.setattr(settings, "ai_case_copilot_enabled", True)

    class _Service:
        def answer(self, *args, **kwargs):
            return CopilotResult(
                status="CONTROL_INTENT_REQUIRED",
                answer="该请求属于受控操作，Case Copilot 不直接执行。",
                proposal={"requested_control_intent": "STOP_REPRODUCTION"},
                grounding={"status": "NOT_APPLICABLE"},
                record_id="copilot-control",
                routed_control_intent="STOP_REPRODUCTION",
            )

    monkeypatch.setattr(api, "CaseCopilotService", _Service)
    with _db() as db:
        case = _case(db)
        result = api.ask_case_copilot(
            case.id,
            api.CaseCopilotRequest(question="停止复现", request_id="req-control"),
            db=db,
            identity=_identity(UserRole.ENGINEER),
        )
        assert result["status"] == "CONTROL_INTENT_REQUIRED"
        assert result["proposal"] is None
        assert result["routed_control_intent"] == "STOP_REPRODUCTION"
        assert result["read_only"] is True


def test_copilot_api_runtime_failure_rolls_back_sidecar_and_returns_safe_503(monkeypatch):
    monkeypatch.setattr(settings, "ai_case_copilot_enabled", True)

    class _FailingService:
        def answer(self, db_arg, **kwargs):
            db_arg.add(AICaseCopilotRecord(
                case_id=kwargs["case_id"],
                request_key="transient-api-runtime-row",
                actor_id=kwargs["actor_id"],
                actor_role=kwargs["actor_role"].value,
                question_hash="c" * 64,
                snapshot_fingerprint="d" * 64,
                status="ANSWERED",
                proposal_json={},
                grounding_report_json={},
                prompt_version="ai-case-copilot-v1",
            ))
            db_arg.flush()
            raise RuntimeError("synthetic API runtime failure")

    monkeypatch.setattr(api, "CaseCopilotService", _FailingService)
    with _db() as db:
        case = _case(db)
        try:
            api.ask_case_copilot(
                case.id,
                api.CaseCopilotRequest(question="现在证据说明什么？", request_id="req-runtime"),
                db=db,
                identity=_identity(UserRole.ENGINEER),
            )
        except Exception as exc:
            assert getattr(exc, "status_code", None) == 503
            assert getattr(exc, "detail", None) == "AI_CASE_COPILOT_RUNTIME_FAILED"
        else:
            raise AssertionError("unexpected AI3 runtime error must return safe 503")

        assert db.get(Case, case.id) is not None
        assert db.scalar(select(AICaseCopilotRecord).where(
            AICaseCopilotRecord.request_key == "transient-api-runtime-row"
        )) is None
