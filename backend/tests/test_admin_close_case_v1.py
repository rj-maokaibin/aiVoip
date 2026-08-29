from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from app.contracts.enums import CaseStatus
from app.core.errors import AppError
from app.db.models import Case, CaseStateHistory
from app.services import case_transitions
from app.services.case_transitions import ADMIN_CLOSE_EVENT, CaseTransitionService
from tools import admin_close_case


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "admin-close-case-live.yml"


class _FakeDb:
    def __init__(self):
        self.added = []

    def add(self, row):
        self.added.append(row)

    def flush(self):
        return None


def _case(status: str = CaseStatus.ANALYZING.value) -> Case:
    return Case(id="case-old", case_no="VOIP-20260829-4C13B1", summary="stale case", status=status)


def test_administrative_close_is_audited_without_fake_fix(monkeypatch):
    audits = []
    monkeypatch.setattr(case_transitions, "audit", lambda *args, **kwargs: audits.append(kwargs))
    db = _FakeDb()
    case = _case()

    CaseTransitionService.administrative_close(
        db,
        case,
        actor="github-admin:rj-maokaibin",
        reason="close stale acceptance case",
        context={"reason_code": "ADMIN_CLOSED_STALE_CASE_FOR_CONVERSATION_ACCEPTANCE"},
    )

    assert case.status == CaseStatus.CLOSED.value
    histories = [row for row in db.added if isinstance(row, CaseStateHistory)]
    assert len(histories) == 1
    assert histories[0].event == ADMIN_CLOSE_EVENT
    assert histories[0].from_status == CaseStatus.ANALYZING.value
    assert histories[0].to_status == CaseStatus.CLOSED.value
    assert histories[0].context_json["preserves_fix_semantics"] is True
    assert "FIX_VERIFIED" not in histories[0].event
    assert histories[0].to_status != CaseStatus.RESOLVED.value
    assert audits and audits[0]["detail"]["event"] == ADMIN_CLOSE_EVENT


def test_administrative_close_requires_controlled_actor_and_reason(monkeypatch):
    monkeypatch.setattr(case_transitions, "audit", lambda *args, **kwargs: None)
    with pytest.raises(AppError) as forbidden:
        CaseTransitionService.administrative_close(
            _FakeDb(), _case(), actor="feishu:user", reason="close"
        )
    assert forbidden.value.code == "CASE_ADMIN_CLOSE_FORBIDDEN"

    with pytest.raises(AppError) as missing_reason:
        CaseTransitionService.administrative_close(
            _FakeDb(), _case(), actor="github-admin:owner", reason=""
        )
    assert missing_reason.value.code == "CASE_ADMIN_CLOSE_REASON_REQUIRED"


def test_administrative_close_preserves_failed_and_is_idempotent_for_closed(monkeypatch):
    monkeypatch.setattr(case_transitions, "audit", lambda *args, **kwargs: None)
    with pytest.raises(AppError) as failed_case:
        CaseTransitionService.administrative_close(
            _FakeDb(), _case(CaseStatus.FAILED.value), actor="github-admin:owner", reason="close"
        )
    assert failed_case.value.code == "CASE_ADMIN_CLOSE_FAILED_CASE_FORBIDDEN"

    db = _FakeDb()
    case = _case(CaseStatus.CLOSED.value)
    returned = CaseTransitionService.administrative_close(
        db, case, actor="github-admin:owner", reason="already closed"
    )
    assert returned is case
    assert db.added == []


def test_production_tool_blocks_active_reproduction_before_mutation():
    source = inspect.getsource(admin_close_case.close_case)
    assert "if active:" in source
    assert "BLOCKED_ACTIVE_REPRODUCTION" in source
    assert source.index("if active:") < source.index("CaseTransitionService.administrative_close")
    assert "close_binding_lifecycle" in source
    assert "ADMIN_CLOSE_VERIFY_FEISHU_BINDING_STILL_ACTIVE" in source
    assert "FIX_VERIFIED" not in source


def test_production_tool_sanitizes_feishu_identifiers():
    assert admin_close_case._sha("oc_sensitive") != "oc_sensitive"
    assert admin_close_case._sha("tenant_sensitive") != "tenant_sensitive"
    assert admin_close_case._sha(None) is None


def test_live_workflow_is_owner_only_exact_master_and_fixed_reason():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "github.event.comment.user.login == github.repository_owner" in workflow
    assert "/admin-close-case " in workflow
    assert "git rev-parse HEAD" in workflow
    assert "tools/admin_close_case.py" in workflow
    assert "--apply" in workflow
    assert "ADMIN_CLOSED_STALE_CASE_FOR_CONVERSATION_ACCEPTANCE" in workflow
    assert "actions/upload-artifact@v4" in workflow
    assert "source \"$ADMIN_CLOSE_RUNTIME_ROOT/production_db.env\"" in workflow
    for forbidden in ["UPDATE cases SET", "FIX_VERIFIED", "RESOLVED --", "docker exec"]:
        assert forbidden not in workflow
