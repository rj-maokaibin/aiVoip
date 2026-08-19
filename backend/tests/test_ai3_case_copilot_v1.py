from __future__ import annotations

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.contracts.enums import UserRole
from app.copilot.service import CaseCopilotService
from app.copilot.snapshot import CaseIntelligenceSnapshotBuilder
from app.db.ai_intelligence_models import AICaseCopilotRecord
from app.db.base import Base
from app.db.models import Case


class FakeEvidenceBuilder:
    def build(self, db, case_id):
        return {
            "case": {"id": case_id, "case_no": "CASE-COPILOT", "summary": "周期性电流音", "status": "ANALYZING"},
            "devices": [{"id": "dev-1", "ip": "192.168.1.2", "ssh_port": 22, "sn": "SN-SECRET", "platform_id": "p1", "device_info": {"product": "T18", "secret": "x"}}],
            "evidences": [
                {"id": "ev-raw", "type": "PCAP", "source": "UPLOAD", "filename": "private.pcap", "sha256": "abc", "kind": "RAW", "scope": "CALL", "level": "L1", "completeness": "COMPLETE", "metadata": {"secret": "meta"}},
                {"id": "ev-1", "type": "PACKET_ANALYSIS", "source": "ANALYZER", "filename": "packet.json", "sha256": "def", "kind": "DERIVED", "scope": "CASE", "level": "L2", "completeness": "COMPLETE", "metadata": {"summary": True}},
            ],
            "analyzers": {"PACKET": {"run_id": "run-1", "status": "SUCCESS", "version": "1", "input_evidence_ids": ["ev-raw", "ev-1"], "summary": {"loss": 1}, "result": {"raw": "hidden"}}},
            "fingerprint": "f" * 64,
        }


class FakeSnapshotBuilder:
    def build(self, db, case_id, *, role):
        return {
            "schema_version": "case-intelligence-snapshot-v1",
            "case": {"id": case_id, "case_no": "CASE-COPILOT", "summary": "周期性电流音", "status": "ANALYZING"},
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


class FakeGateway:
    def __init__(self, proposal):
        self.proposal = proposal
        self.calls = 0

    def answer(self, **kwargs):
        self.calls += 1
        return {"proposal": self.proposal, "model": "copilot-test", "prompt_version": "ai-case-copilot-v1"}


class ForbiddenGateway:
    def answer(self, **kwargs):
        raise AssertionError("control requests must not call AI")


def _db():
    engine = create_engine("sqlite+pysqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return Session(engine)


def _case(db):
    row = Case(case_no="CASE-COPILOT", summary="周期性电流音", status="ANALYZING")
    db.add(row)
    db.commit()
    return row


def _proposal(*, evidence_id="ev-1", evidence_level="L5", status="PROPOSED", answer="当前证据显示 RTP 侧存在异常，但根因尚未确认。"):
    return {
        "schema_version": "ai-case-copilot-v1",
        "answer": answer,
        "claims": [{
            "claim_id": "claim-1",
            "claim_type": "OBSERVATION",
            "statement": "RTP 侧存在异常",
            "subject": "RTP",
            "predicate": "has_anomaly",
            "value": True,
            "status": status,
            "evidence_level": evidence_level,
            "evidence": [{"evidence_id": evidence_id, "relation": "SUPPORT", "direction": "UNKNOWN", "note": "current Case evidence"}],
            "missing_evidence": [],
        }],
        "cited_evidence_ids": [evidence_id],
        "uncertainty": ["根因尚未确认"],
        "next_steps": [{"kind": "READ_ONLY_GUIDANCE", "text": "继续核对同一时间窗的 PCM 与 RTP。"}],
        "root_cause_confirmed_by_ai": False,
        "safety_class": "READ_ONLY_GROUNDED_RESPONSE",
    }


def test_snapshot_viewer_cannot_see_raw_evidence_and_device_access_identifiers():
    with _db() as db:
        case = _case(db)
        builder = CaseIntelligenceSnapshotBuilder(FakeEvidenceBuilder())
        viewer = builder.build(db, case.id, role=UserRole.VIEWER)
        engineer = builder.build(db, case.id, role=UserRole.ENGINEER)
        assert viewer["raw_evidence_visible"] is False
        assert [x["id"] for x in viewer["evidences"]] == ["ev-1"]
        assert all(str(x.get("kind")).upper() != "RAW" for x in viewer["evidences"])
        assert "filename" not in viewer["evidences"][0]
        assert "sha256" not in viewer["evidences"][0]
        assert viewer["analyzers"]["PACKET"]["input_evidence_ids"] == ["ev-1"]
        assert "result" not in viewer["analyzers"]["PACKET"]
        assert "ip" not in viewer["devices"][0]
        assert "sn" not in viewer["devices"][0]
        assert engineer["raw_evidence_visible"] is True
        assert {x["id"] for x in engineer["evidences"]} == {"ev-raw", "ev-1"}
        assert next(x for x in engineer["evidences"] if x["id"] == "ev-raw")["filename"] == "private.pcap"
        assert engineer["analyzers"]["PACKET"]["result"]["raw"] == "hidden"


def test_grounded_current_case_answer_passes_and_is_persisted():
    with _db() as db:
        case = _case(db)
        gateway = FakeGateway(_proposal())
        service = CaseCopilotService(snapshot_builder=FakeSnapshotBuilder(), gateway=gateway)
        result = service.answer(
            db, case_id=case.id, question="目前证据说明了什么？", request_key="req-1",
            actor_id="engineer-1", actor_role=UserRole.ENGINEER,
        )
        assert result.status == "ANSWERED"
        assert result.grounding["status"] == "PASS"
        assert gateway.calls == 1
        row = db.scalar(select(AICaseCopilotRecord).where(AICaseCopilotRecord.request_key == "req-1"))
        assert row is not None
        assert len(row.question_hash) == 64
        assert row.status == "ANSWERED"


def test_cross_case_evidence_reference_is_rejected():
    with _db() as db:
        case = _case(db)
        service = CaseCopilotService(snapshot_builder=FakeSnapshotBuilder(), gateway=FakeGateway(_proposal(evidence_id="ev-other")))
        result = service.answer(
            db, case_id=case.id, question="当前异常是什么？", request_key="req-cross",
            actor_id="viewer-1", actor_role=UserRole.VIEWER,
        )
        assert result.status == "REJECTED"
        assert "COPILOT_EVIDENCE_NOT_IN_CASE" in (result.error_code or "") or "COPILOT_GROUNDING_NOT_PASS" in (result.error_code or "")


def test_ai_cannot_promote_evidence_level_or_claim_status():
    with _db() as db:
        case = _case(db)
        for suffix, proposal in [
            ("level", _proposal(evidence_level="L3")),
            ("status", _proposal(status="SUPPORTED")),
        ]:
            service = CaseCopilotService(snapshot_builder=FakeSnapshotBuilder(), gateway=FakeGateway(proposal))
            result = service.answer(
                db, case_id=case.id, question="现在能确认吗？", request_key=f"req-{suffix}",
                actor_id="engineer-1", actor_role=UserRole.ENGINEER,
            )
            assert result.status == "REJECTED"
            assert "COPILOT_GROUNDING_NOT_PASS" in (result.error_code or "")


def test_root_cause_confirmation_language_is_rejected_by_contract():
    with _db() as db:
        case = _case(db)
        service = CaseCopilotService(
            snapshot_builder=FakeSnapshotBuilder(),
            gateway=FakeGateway(_proposal(answer="最终根因是话柄硬件问题。")),
        )
        result = service.answer(
            db, case_id=case.id, question="根因是什么？", request_key="req-root-cause",
            actor_id="reviewer-1", actor_role=UserRole.EXPERT_REVIEWER,
        )
        assert result.status == "REJECTED"
        assert "COPILOT_ROOT_CAUSE_CONFIRMATION_FORBIDDEN" in (result.error_code or "")


def test_control_request_routes_out_without_calling_model_or_action_worker():
    with _db() as db:
        case = _case(db)
        service = CaseCopilotService(snapshot_builder=FakeSnapshotBuilder(), gateway=ForbiddenGateway())
        result = service.answer(
            db, case_id=case.id, question="请停止复现", request_key="req-control",
            actor_id="engineer-1", actor_role=UserRole.ENGINEER,
        )
        assert result.status == "CONTROL_INTENT_REQUIRED"
        assert result.routed_control_intent == "STOP_REPRODUCTION"
        assert "不直接执行" in result.answer


def test_duplicate_request_key_is_idempotent_and_calls_model_once():
    with _db() as db:
        case = _case(db)
        gateway = FakeGateway(_proposal())
        service = CaseCopilotService(snapshot_builder=FakeSnapshotBuilder(), gateway=gateway)
        one = service.answer(db, case_id=case.id, question="证据？", request_key="req-dup", actor_id="e1", actor_role=UserRole.ENGINEER)
        two = service.answer(db, case_id=case.id, question="证据？", request_key="req-dup", actor_id="e1", actor_role=UserRole.ENGINEER)
        assert one.record_id == two.record_id
        assert gateway.calls == 1
