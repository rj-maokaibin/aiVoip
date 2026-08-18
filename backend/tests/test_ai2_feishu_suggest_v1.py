from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.feishu_permissions import FeishuCapability, authorize_capability
from app.contracts.enums import UserRole
from app.core.config import settings
from app.db.ai_intelligence_models import AIDiagnosticCycle
from app.db.base import Base
from app.db.feishu_governance_models import CaseAclEntry, FeishuUserIdentity
from app.db.models import Case
from app.integrations.feishu.authorized_events import _authorize_card_action
from app.integrations.feishu.cards import FeishuCaseCardBuilder
from app.integrations.feishu.events import dispatch_event
from app.integrations.feishu.identity import resolve_feishu_identity


def _db():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return Session(engine)


def _case(db: Session) -> Case:
    row = Case(case_no="CASE-AI2-FEISHU", summary="周期性电流音", status="ANALYZING")
    db.add(row)
    db.flush()
    return row


def _identity(db: Session, *, open_id: str, role: UserRole) -> FeishuUserIdentity:
    row = FeishuUserIdentity(
        tenant_key="tenant-ai2",
        open_id=open_id,
        internal_actor_id=f"actor:{open_id}",
        role=role.value,
        status="ACTIVE",
    )
    db.add(row)
    db.flush()
    return row


def _cycle(case_id: str, *, kind: str = "QUESTION", registered_id: str = "AUDIO_NOISE_FAULT_LAYER") -> AIDiagnosticCycle:
    return AIDiagnosticCycle(
        case_id=case_id,
        cycle_no=1,
        runtime_stage="SUGGEST",
        snapshot_fingerprint="s" * 64,
        evidence_fingerprint="e" * 64,
        proposal_id=None,
        status="COMPLETED",
        known_json=[],
        unknown_json=["仍需补充判别证据"],
        excluded_json=[],
        hypotheses_json=[],
        critic_json={"status": "PASS"},
        next_action_json={
            "type": kind,
            "registered_id": registered_id,
            "reason": "需要下一项已注册判别动作",
            "dispatch_allowed": False,
            "raw_command_allowed": False,
        },
        selection_json={"kind": kind, "registered_id": registered_id, "dispatch_allowed": False},
        continue_recommendation="CONTINUE",
        no_progress_count=0,
        formal_result_changed=False,
        dispatch_attempted=False,
        dispatch_allowed=False,
        suggestion_state="PROPOSED",
    )


def _card_payload(*, open_id: str, case_id: str, cycle_id: str) -> dict:
    return {
        "header": {"event_type": "card.action.trigger", "tenant_key": "tenant-ai2"},
        "operator": {"open_id": open_id},
        "action": {
            "value": {
                "action": "AI2_ACCEPT_SUGGESTION",
                "case_id": case_id,
                "cycle_id": cycle_id,
            }
        },
    }


def test_case_card_surfaces_suggest_as_non_executing_registered_recommendation():
    with _db() as db:
        case = _case(db)
        cycle = _cycle(case.id)
        db.add(cycle)
        db.flush()
        card = FeishuCaseCardBuilder().build(db, case.id).card
        text = str(card)
        assert "AI2 下一步建议（SUGGEST）" in text
        assert "AUDIO_NOISE_FAULT_LAYER" in text
        assert "采纳 AI2 建议" in text
        assert "不是 Root Cause" in text
        assert "AI 不自动执行" in text
        assert "ssh root@" not in text
        assert "raw_command" not in text


def test_viewer_cannot_accept_ai2_suggestion_even_for_question():
    with _db() as db:
        case = _case(db)
        cycle = _cycle(case.id)
        db.add(cycle)
        _identity(db, open_id="ou-viewer", role=UserRole.VIEWER)
        db.flush()
        identity = resolve_feishu_identity(db, tenant_key="tenant-ai2", open_id="ou-viewer")
        decision = authorize_capability(
            db,
            identity=identity,
            capability=FeishuCapability.RUN_AI_SUGGESTION,
            case_id=case.id,
        )
        assert decision.allowed is False
        assert decision.reason == "GLOBAL_ROLE_MISSING_CAPABILITY"

        _identity_ctx, denied = _authorize_card_action(
            db,
            _card_payload(open_id="ou-viewer", case_id=case.id, cycle_id=cycle.id),
        )
        assert denied is not None
        assert denied["handled"] == "permission_denied"
        assert denied["capability"] == "RUN_AI_SUGGESTION"


def test_engineer_still_respects_case_acl_deny_on_underlying_reproduction_control():
    with _db() as db:
        case = _case(db)
        cycle = _cycle(case.id, kind="REPRODUCTION_PROFILE", registered_id="audio_noise_deep_capture")
        db.add(cycle)
        identity_row = _identity(db, open_id="ou-engineer", role=UserRole.ENGINEER)
        db.add(CaseAclEntry(
            case_id=case.id,
            actor_id=identity_row.internal_actor_id,
            capability=FeishuCapability.CONTROL_REPRODUCTION.value,
            effect="DENY",
            created_by="admin",
        ))
        db.flush()

        identity, denied = _authorize_card_action(
            db,
            _card_payload(open_id="ou-engineer", case_id=case.id, cycle_id=cycle.id),
        )
        assert identity.active is True
        assert denied is not None
        assert denied["handled"] == "permission_denied"
        assert denied["capability"] == "CONTROL_REPRODUCTION"
        assert denied["reason"] == "CASE_ACL_DENY"


def test_engineer_question_suggestion_passes_both_authorization_layers():
    with _db() as db:
        case = _case(db)
        cycle = _cycle(case.id)
        db.add(cycle)
        _identity(db, open_id="ou-engineer", role=UserRole.ENGINEER)
        db.flush()
        identity, denied = _authorize_card_action(
            db,
            _card_payload(open_id="ou-engineer", case_id=case.id, cycle_id=cycle.id),
        )
        assert identity.active is True
        assert identity.role == UserRole.ENGINEER
        assert denied is None


def test_direct_ai2_card_dispatch_fails_closed_when_g2_rbac_is_disabled(monkeypatch):
    monkeypatch.setattr(settings, "feishu_identity_rbac_enabled", False)
    with _db() as db:
        case = _case(db)
        cycle = _cycle(case.id)
        db.add(cycle)
        db.flush()
        result = dispatch_event(
            db,
            payload=_card_payload(open_id="ou-any", case_id=case.id, cycle_id=cycle.id),
            actor="actor:direct",
        )
        assert result["handled"] == "error"
        assert result["reason"] == "AI2_SUGGESTION_RBAC_REQUIRED"
        assert cycle.suggestion_state == "PROPOSED"
        assert cycle.execution_ref_id is None
