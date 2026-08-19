from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.v1 import ai_copilot as api
from app.auth.providers import AuthIdentity
from app.contracts.enums import UserRole
from app.copilot.service import CaseCopilotService, CopilotResult
from app.core.config import settings
from app.db.base import Base
from app.db.models import Case


class _SnapshotBuilder:
    def build(self, db, case_id, *, role):
        return {
            "schema_version": "case-intelligence-snapshot-v1",
            "case": {"id": case_id, "case_no": "CASE-AI3-IDEMP", "status": "ANALYZING"},
            "viewer_role": role.value,
            "raw_evidence_visible": role != UserRole.VIEWER,
            "devices": [],
            "evidences": [{"id": "ev-1", "type": "PACKET_ANALYSIS", "kind": "DERIVED", "level": "L2", "completeness": "COMPLETE"}],
            "analyzers": {},
            "preliminary_report": None,
            "diagnosis": None,
            "reproductions": [],
            "experiments": [],
            "fix_verifications": [],
            "authority": {"ai_can_confirm_root_cause": False},
            "fingerprint": "f" * 64,
        }

    @staticmethod
    def allowed_evidence_ids(snapshot):
        return {str(x["id"]) for x in snapshot.get("evidences") or []}


class _Gateway:
    def __init__(self):
        self.calls = 0

    def answer(self, **kwargs):
        self.calls += 1
        return {
            "model": "ai3-idempotency-test",
            "prompt_version": "ai-case-copilot-v1",
            "proposal": {
                "schema_version": "ai-case-copilot-v1",
                "answer": "当前证据显示 RTP 侧存在异常，但根因尚未确认。",
                "claims": [{
                    "claim_id": "claim-1",
                    "claim_type": "OBSERVATION",
                    "statement": "RTP 侧存在异常",
                    "subject": "RTP",
                    "predicate": "has_anomaly",
                    "value": True,
                    "status": "PROPOSED",
                    "evidence_level": "L5",
                    "evidence": [{"evidence_id": "ev-1", "relation": "SUPPORT", "direction": "UNKNOWN", "note": "current Case evidence"}],
                    "missing_evidence": [],
                }],
                "cited_evidence_ids": ["ev-1"],
                "uncertainty": ["根因尚未确认"],
                "next_steps": [],
                "root_cause_confirmed_by_ai": False,
                "safety_class": "READ_ONLY_GROUNDED_RESPONSE",
            },
        }


def _db():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return Session(engine)


def _case(db):
    row = Case(case_no="CASE-AI3-IDEMP", summary="权限隔离测试", status="ANALYZING")
    db.add(row)
    db.commit()
    return row


def test_service_rejects_cross_actor_request_key_replay():
    with _db() as db:
        case = _case(db)
        gateway = _Gateway()
        service = CaseCopilotService(snapshot_builder=_SnapshotBuilder(), gateway=gateway)
        first = service.answer(db, case_id=case.id, question="当前证据说明什么？", request_key="shared-key", actor_id="engineer-a", actor_role=UserRole.ENGINEER)
        assert first.status == "ANSWERED"
        with pytest.raises(ValueError, match="COPILOT_REQUEST_KEY_ACTOR_CONFLICT"):
            service.answer(db, case_id=case.id, question="当前证据说明什么？", request_key="shared-key", actor_id="viewer-b", actor_role=UserRole.VIEWER)
        assert gateway.calls == 1


def test_service_rejects_cross_role_request_key_replay():
    with _db() as db:
        case = _case(db)
        gateway = _Gateway()
        service = CaseCopilotService(snapshot_builder=_SnapshotBuilder(), gateway=gateway)
        first = service.answer(db, case_id=case.id, question="当前证据说明什么？", request_key="same-actor-role-change", actor_id="user-a", actor_role=UserRole.ENGINEER)
        assert first.status == "ANSWERED"
        with pytest.raises(ValueError, match="COPILOT_REQUEST_KEY_ROLE_CONFLICT"):
            service.answer(db, case_id=case.id, question="当前证据说明什么？", request_key="same-actor-role-change", actor_id="user-a", actor_role=UserRole.VIEWER)
        assert gateway.calls == 1


def test_service_rejects_same_request_key_with_different_question():
    with _db() as db:
        case = _case(db)
        gateway = _Gateway()
        service = CaseCopilotService(snapshot_builder=_SnapshotBuilder(), gateway=gateway)
        first = service.answer(db, case_id=case.id, question="问题 A", request_key="same-key-different-question", actor_id="user-a", actor_role=UserRole.ENGINEER)
        assert first.status == "ANSWERED"
        with pytest.raises(ValueError, match="COPILOT_REQUEST_KEY_QUESTION_CONFLICT"):
            service.answer(db, case_id=case.id, question="问题 B", request_key="same-key-different-question", actor_id="user-a", actor_role=UserRole.ENGINEER)
        assert gateway.calls == 1


def test_api_scopes_idempotency_key_to_actor_and_role_with_bounded_hash(monkeypatch):
    monkeypatch.setattr(settings, "ai_case_copilot_enabled", True)
    keys = []

    class _Service:
        def answer(self, db_arg, **kwargs):
            keys.append(kwargs["request_key"])
            return CopilotResult(
                status="ANSWERED",
                answer="仅用于验证幂等键。",
                proposal={
                    "schema_version": "ai-case-copilot-v1",
                    "answer": "仅用于验证幂等键。",
                    "claims": [],
                    "cited_evidence_ids": [],
                    "uncertainty": [],
                    "next_steps": [],
                    "root_cause_confirmed_by_ai": False,
                    "safety_class": "READ_ONLY_GROUNDED_RESPONSE",
                },
                grounding={"status": "PASS"},
                record_id="test-record",
            )

    monkeypatch.setattr(api, "CaseCopilotService", _Service)
    with _db() as db:
        case = _case(db)
        req = api.CaseCopilotRequest(question="同一个 request id", request_id="x" * 192)
        api.ask_case_copilot(case.id, req, db=db, identity=AuthIdentity("actor-a", UserRole.ENGINEER, True, "test"))
        api.ask_case_copilot(case.id, req, db=db, identity=AuthIdentity("actor-b", UserRole.VIEWER, True, "test"))

    assert len(keys) == 2
    assert keys[0] != keys[1]
    assert all(key.startswith("api:") for key in keys)
    assert all(len(key) == 68 for key in keys)
    assert all("actor-" not in key for key in keys)
