from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.v1 import ai_semantic as api
from app.auth.providers import AuthIdentity
from app.contracts.enums import UserRole
from app.db.base import Base
from app.db.models import Case
from app.integrations.feishu.semantic_router import SemanticShadowResult


def _db():
    engine = create_engine("sqlite+pysqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return Session(engine)


def _admin():
    return AuthIdentity("admin-ai1", UserRole.ADMIN, True, "test")


def test_semantic_debug_api_returns_shadow_without_execution_authority(monkeypatch):
    with _db() as db:
        case = Case(case_no="CASE-AI1-API", summary="电流音", status="ANALYZING")
        db.add(case)
        db.commit()
        seen = {}

        def fake_shadow(db_arg, **kwargs):
            seen.update(kwargs)
            return SemanticShadowResult(
                status="SHADOW_VALID",
                final_intent="CASE_FOLLOW_UP",
                deterministic_intent="CASE_FOLLOW_UP",
                proposal={
                    "schema_version": "feishu-semantic-intent-v1",
                    "intent": "CASE_FOLLOW_UP",
                    "safety_class": "NON_EXECUTING_PROPOSAL",
                    "confidence": 0.95,
                },
                record_id="semantic-1",
            )

        monkeypatch.setattr(api, "shadow_semantic_route", fake_shadow)
        result = api.resolve_semantic_intent(
            case.id,
            api.SemanticResolveRequest(
                text="又复现了，换回原装就正常",
                message_id="manual-message-1",
                tenant_key="tenant-a",
                chat_id="oc-chat-a",
            ),
            db=db,
            _identity=_admin(),
        )
        assert result["status"] == "SHADOW_VALID"
        assert result["final_intent"] == "CASE_FOLLOW_UP"
        assert result["execution_authority"] == "DETERMINISTIC_ROUTER_RBAC_POLICY"
        assert result["semantic_proposal_is_non_executing"] is True
        assert seen["force"] is True
        assert seen["case_id"] == case.id
        assert seen["case_no"] == case.case_no
