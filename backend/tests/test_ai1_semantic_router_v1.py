from __future__ import annotations

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.contracts.semantic_intent import validate_semantic_proposal
from app.db.ai_intelligence_models import AISemanticIntentRecord
from app.db.base import Base
from app.db.models import Case
from app.integrations.feishu.intake import route_intake
from app.integrations.feishu.semantic_router import needs_semantic_fallback, shadow_semantic_route


class FakeGateway:
    def __init__(self, proposal):
        self.proposal = proposal
        self.calls = 0

    def resolve(self, **kwargs):
        self.calls += 1
        return {"proposal": self.proposal, "model": "fake-semantic", "prompt_version": "feishu-semantic-router-v1"}


class ForbiddenGateway:
    def resolve(self, **kwargs):
        raise AssertionError("gateway must not be called")


def _db():
    engine = create_engine("sqlite+pysqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return Session(engine)


def _case(db: Session):
    row = Case(case_no="CASE-AI1-001", summary="周期性电流音", status="ANALYZING")
    db.add(row)
    db.commit()
    return row


def _proposal(**overrides):
    base = {
        "schema_version": "feishu-semantic-intent-v1",
        "intent": "CASE_FOLLOW_UP",
        "case_operation": "ADD_EVIDENCE_AND_COMPARE",
        "case_ref": None,
        "symptoms": ["PERIODIC_NOISE"],
        "device_refs": [],
        "environment_changes": {"handset": {"from": "deli", "to": "original"}},
        "temporal_clues": {"onset": "10-20s after call start"},
        "attachment_roles": [{"attachment_id": "att-1", "role": "NEW_REPRODUCTION_EVIDENCE"}],
        "comparison_request": {"compare_with_previous_environment": True},
        "requested_operation": "CONTINUE_ANALYSIS",
        "confidence": 0.94,
        "missing_fields": [],
        "safety_class": "NON_EXECUTING_PROPOSAL",
    }
    base.update(overrides)
    return base


def test_explicit_stop_is_deterministic_bypass_and_never_calls_ai():
    with _db() as db:
        intake = route_intake(text="停止复现", attachments=[], has_thread_case=True)
        result = shadow_semantic_route(
            db, message_id="m-stop", text="停止复现", attachments=[], deterministic=intake,
            case_id=None, gateway=ForbiddenGateway(), force=True,
        )
        assert result.status == "BYPASSED"
        assert result.final_intent == "STOP_REPRODUCTION"
        assert result.proposal is None


def test_complex_environment_change_triggers_shadow_but_cannot_change_final_intent():
    with _db() as db:
        case = _case(db)
        attachments = [{"file_key": "att-1", "filename": "new.pcap", "message_type": "file"}]
        text = "又复现了，换回原装就正常，附件是新的包，帮我和上一次对比"
        intake = route_intake(text=text, attachments=attachments, has_thread_case=True)
        assert needs_semantic_fallback(text=text, deterministic=intake)
        gateway = FakeGateway(_proposal(intent="STOP_REPRODUCTION"))
        result = shadow_semantic_route(
            db, message_id="m-complex", text=text, attachments=attachments,
            deterministic=intake, case_id=case.id, case_no=case.case_no,
            tenant_key="tenant-a", chat_id="oc-chat", gateway=gateway, force=True,
        )
        assert result.status == "SHADOW_VALID"
        assert result.proposal["intent"] == "STOP_REPRODUCTION"
        assert result.final_intent == intake.intent
        assert result.final_intent != result.proposal["intent"]
        row = db.scalar(select(AISemanticIntentRecord).where(AISemanticIntentRecord.message_id == "m-complex"))
        assert row is not None and row.status == "SHADOW_VALID"
        assert row.input_hash and len(row.input_hash) == 64
        assert gateway.calls == 1


def test_ai_case_override_is_rejected_after_g1_resolution():
    with _db() as db:
        case = _case(db)
        text = "又复现了，换回原装就正常"
        intake = route_intake(text=text, attachments=[], has_thread_case=True)
        result = shadow_semantic_route(
            db, message_id="m-case-override", text=text, attachments=[], deterministic=intake,
            case_id=case.id, case_no=case.case_no,
            gateway=FakeGateway(_proposal(case_ref="CASE-OTHER-999")), force=True,
        )
        assert result.status == "REJECTED"
        assert result.final_intent == intake.intent
        assert "SEMANTIC_CASE_OVERRIDE_FORBIDDEN" in (result.error_code or "")


def test_low_confidence_semantic_proposal_fails_closed():
    with _db() as db:
        case = _case(db)
        text = "又复现了，环境好像变了"
        intake = route_intake(text=text, attachments=[], has_thread_case=True)
        result = shadow_semantic_route(
            db, message_id="m-low", text=text, attachments=[], deterministic=intake,
            case_id=case.id, case_no=case.case_no,
            gateway=FakeGateway(_proposal(confidence=0.30)), force=True,
        )
        assert result.status == "REJECTED"
        assert "SEMANTIC_CONFIDENCE_BELOW_THRESHOLD" in (result.error_code or "")
        assert result.final_intent == intake.intent


def test_duplicate_message_id_replays_one_semantic_record_and_one_gateway_call():
    with _db() as db:
        case = _case(db)
        text = "又复现了，换回原装就正常"
        intake = route_intake(text=text, attachments=[], has_thread_case=True)
        gateway = FakeGateway(_proposal())
        one = shadow_semantic_route(
            db, message_id="m-dup", text=text, attachments=[], deterministic=intake,
            case_id=case.id, case_no=case.case_no, gateway=gateway, force=True,
        )
        two = shadow_semantic_route(
            db, message_id="m-dup", text=text, attachments=[], deterministic=intake,
            case_id=case.id, case_no=case.case_no, gateway=gateway, force=True,
        )
        assert one.record_id == two.record_id
        assert gateway.calls == 1
        assert len(list(db.scalars(select(AISemanticIntentRecord)))) == 1


def test_schema_rejects_raw_command_or_extra_execution_field():
    payload = _proposal()
    payload["raw_command"] = "ssh root@device reboot"
    try:
        validate_semantic_proposal(payload)
    except Exception:
        pass
    else:
        raise AssertionError("raw execution field must be rejected")

    payload = _proposal(environment_changes={"note": "AIM> voip dsp diag set 1.1.1.1 40000 1 pcm_rx on"})
    try:
        validate_semantic_proposal(payload)
    except Exception:
        pass
    else:
        raise AssertionError("command material hidden in semantic fields must be rejected")
