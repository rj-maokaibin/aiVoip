from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db import models as _models  # noqa: F401
from app.db import conversation_models as _conversation_models  # noqa: F401
from app.db import knowledge_models as _knowledge_models  # noqa: F401
from app.db.conversation_models import Conversation, ConversationTurn
from app.db.knowledge_models import ProductFact
from app.conversation.orchestrator import AssistantConversationOrchestrator


def _db():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return Session(engine)


def _ctx(mid):
    return {
        "tenant_key": "tenant-a",
        "chat_id": "chat-a",
        "message_id": mid,
        "sender_open_id": "ou-a",
    }


def _fact(version, supported=True):
    return ProductFact(
        product_model="T18",
        feature_key="VOIP.DTMF.RFC2833",
        value_json={"supported": supported},
        value_text="支持" if supported else "不支持",
        sw_version_scope=version,
        hw_scope="*",
        region_scope="*",
        source_document=f"T18 {version} SPEC",
        authority_level=3,
        approval_status="APPROVED",
    )


def test_no_case_knowledge_turn_persists_without_case_or_evidence():
    with _db() as db:
        db.add(_fact("R412", True))
        db.commit()
        result = AssistantConversationOrchestrator().prepare_turn(
            db,
            text="T18 R412 RFC2833 支持吗？",
            source_context=_ctx("m1"),
            case_id=None,
            case_context=None,
        )
        db.commit()
        assert result.material_diagnostic_context is False
        assert result.interpretation["intent"] == "KNOWLEDGE_QUERY"
        assert "支持" in (result.response_text or "")
        assert db.query(Conversation).count() == 1
        assert db.query(ConversationTurn).count() == 1
        turn = db.scalar(select(ConversationTurn))
        assert turn.case_id is None
        assert turn.evidence_id is None


def test_followup_version_reuses_product_and_feature_context():
    with _db() as db:
        db.add_all([_fact("R412", True), _fact("R413", False)])
        db.commit()
        orch = AssistantConversationOrchestrator()
        first = orch.prepare_turn(
            db,
            text="T18 R412 RFC2833 支持吗？",
            source_context=_ctx("m1"),
            case_id=None,
            case_context=None,
        )
        db.commit()
        second = orch.prepare_turn(
            db,
            text="R413 呢？",
            source_context=_ctx("m2"),
            case_id=None,
            case_context=None,
        )
        db.commit()
        assert "支持" in (first.response_text or "")
        assert "不支持" in (second.response_text or "")
        conversation = db.scalar(select(Conversation))
        assert conversation.entities_json["product_model"] == "T18"
        assert conversation.entities_json["feature_key"] == "VOIP.DTMF.RFC2833"
        assert conversation.entities_json["software_version"] == "R413"
        assert db.query(ConversationTurn).count() == 2


def test_unreviewed_product_fact_cannot_become_answer():
    with _db() as db:
        fact = _fact("R412", True)
        fact.approval_status = "DRAFT"
        db.add(fact)
        db.commit()
        result = AssistantConversationOrchestrator().prepare_turn(
            db,
            text="T18 R412 RFC2833 支持吗？",
            source_context=_ctx("m1"),
            case_id=None,
            case_context=None,
        )
        assert "已审核知识库中没有找到" in (result.response_text or "")
