from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.contracts.enums import UserRole
from app.copilot.service import CaseCopilotService
from app.db.base import Base
from app.db.models import Case


class ForbiddenGateway:
    def answer(self, **kwargs):
        raise AssertionError("gateway must not be called")


class NoEvidenceSnapshot:
    def build(self, db, case_id, *, role):
        return {
            "schema_version": "case-intelligence-snapshot-v1",
            "case": {"id": case_id, "case_no": "CASE-AI3-NO-EV", "summary": "问题", "status": "ANALYZING"},
            "viewer_role": role.value,
            "raw_evidence_visible": False,
            "devices": [],
            "evidences": [],
            "analyzers": {},
            "preliminary_report": None,
            "diagnosis": None,
            "reproductions": [],
            "experiments": [],
            "fix_verifications": [],
            "authority": {"ai_can_confirm_root_cause": False},
            "fingerprint": "0" * 64,
        }

    @staticmethod
    def allowed_evidence_ids(snapshot):
        return set()


class EvidenceSnapshot:
    def build(self, db, case_id, *, role):
        return {
            "schema_version": "case-intelligence-snapshot-v1",
            "case": {"id": case_id, "case_no": "CASE-AI3-EV", "summary": "问题", "status": "ANALYZING"},
            "viewer_role": role.value,
            "raw_evidence_visible": True,
            "devices": [],
            "evidences": [
                {"id": "ev-1", "type": "PACKET_ANALYSIS", "kind": "DERIVED", "level": "L2"},
                {"id": "ev-2", "type": "PCM_ANALYSIS", "kind": "DERIVED", "level": "L2"},
            ],
            "analyzers": {},
            "preliminary_report": None,
            "diagnosis": None,
            "reproductions": [],
            "experiments": [],
            "fix_verifications": [],
            "authority": {"ai_can_confirm_root_cause": False},
            "fingerprint": "1" * 64,
        }

    @staticmethod
    def allowed_evidence_ids(snapshot):
        return {"ev-1", "ev-2"}


class FakeGateway:
    def __init__(self, proposal):
        self.proposal = proposal

    def answer(self, **kwargs):
        return {"proposal": self.proposal, "model": "fake"}


def _db():
    engine = create_engine("sqlite+pysqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return Session(engine)


def _case(db):
    row = Case(case_no="CASE-AI3-FAIL", summary="问题", status="ANALYZING")
    db.add(row)
    db.commit()
    return row


def _claim(evidence_id: str, claim_id: str):
    return {
        "claim_id": claim_id,
        "claim_type": "OBSERVATION",
        "statement": f"Evidence {evidence_id} supports observation",
        "subject": "media",
        "predicate": "has_observation",
        "value": True,
        "status": "PROPOSED",
        "evidence_level": "L5",
        "evidence": [{"evidence_id": evidence_id, "relation": "SUPPORT", "direction": "UNKNOWN", "note": "current Case"}],
        "missing_evidence": [],
    }


def _proposal(claims, cited):
    return {
        "schema_version": "ai-case-copilot-v1",
        "answer": "当前证据支持异常观察，但根因尚未确认。",
        "claims": claims,
        "cited_evidence_ids": cited,
        "uncertainty": ["根因尚未确认"],
        "next_steps": [],
        "root_cause_confirmed_by_ai": False,
        "safety_class": "READ_ONLY_GROUNDED_RESPONSE",
    }


def test_no_authorized_evidence_rejects_without_calling_model():
    with _db() as db:
        case = _case(db)
        result = CaseCopilotService(snapshot_builder=NoEvidenceSnapshot(), gateway=ForbiddenGateway()).answer(
            db, case_id=case.id, question="目前证据说明什么？", request_key="no-evidence",
            actor_id="viewer", actor_role=UserRole.VIEWER,
        )
        assert result.status == "REJECTED"
        assert result.error_code == "COPILOT_NO_AUTHORIZED_EVIDENCE"


def test_empty_claim_prose_is_rejected_even_with_valid_case_evidence():
    with _db() as db:
        case = _case(db)
        result = CaseCopilotService(
            snapshot_builder=EvidenceSnapshot(),
            gateway=FakeGateway(_proposal([], ["ev-1"])),
        ).answer(
            db, case_id=case.id, question="直接告诉我结论", request_key="empty-claims",
            actor_id="engineer", actor_role=UserRole.ENGINEER,
        )
        assert result.status == "REJECTED"


def test_every_claim_evidence_must_be_present_in_public_citations():
    with _db() as db:
        case = _case(db)
        result = CaseCopilotService(
            snapshot_builder=EvidenceSnapshot(),
            gateway=FakeGateway(_proposal([_claim("ev-1", "c1"), _claim("ev-2", "c2")], ["ev-1"])),
        ).answer(
            db, case_id=case.id, question="说明异常", request_key="missing-public-citation",
            actor_id="engineer", actor_role=UserRole.ENGINEER,
        )
        assert result.status == "REJECTED"
        assert "COPILOT_CLAIM_EVIDENCE_NOT_PUBLICLY_CITED" in (result.error_code or "")
