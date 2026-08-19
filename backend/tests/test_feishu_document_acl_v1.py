import asyncio

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.db.base import Base
from app.db.feishu_governance_models import FeishuDocumentAclBinding
from app.db.models import Case, FeishuCaseBinding
from app.integrations.feishu.document_acl import (
    Collaborator,
    FeishuDocumentAclService,
    FeishuTransportError,
)


class FakeAdapter:
    def __init__(self, *, chat_supported=True, chat_members=None, collaborators=None):
        self.chat_supported = chat_supported
        self.chat_members = list(chat_members or [])
        self.collaborators = list(collaborators or [])
        self.calls = []

    async def bot_in_chat(self, chat_id):
        return True

    async def list_collaborators(self, document_id):
        return list(self.collaborators)

    async def add_collaborator(self, document_id, *, member_type, member_id, perm):
        if member_type == "openchat" and not self.chat_supported:
            raise FeishuTransportError("OPENCHAT_UNSUPPORTED")
        self.calls.append(("add", member_type, member_id, perm))
        self.collaborators.append(Collaborator(member_type, member_id, perm))

    async def update_collaborator(self, document_id, *, member_type, member_id, perm):
        self.calls.append(("update", member_type, member_id, perm))
        self.collaborators = [
            Collaborator(item.member_type, item.member_id, perm)
            if item.member_type == member_type and item.member_id == member_id else item
            for item in self.collaborators
        ]

    async def remove_collaborator(self, document_id, *, member_type, member_id):
        self.calls.append(("remove", member_type, member_id, None))
        self.collaborators = [item for item in self.collaborators
                              if not (item.member_type == member_type and item.member_id == member_id)]

    async def list_chat_members(self, chat_id):
        return list(self.chat_members)


def _db():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return Session(engine)


def _source(db: Session):
    case = Case(case_no="CASE-DOC-ACL", summary="电流音", status="ANALYZING")
    db.add(case); db.flush()
    db.add(FeishuCaseBinding(
        case_id=case.id, receive_id="oc-case", receive_id_type="chat_id",
        source_tenant_key="tenant-a", source_message_id="m-root",
        status="ACTIVE", card_version=0,
    ))
    db.flush()
    return case


def _set(monkeypatch, mode="AUTO", permission="view", fallback=True):
    monkeypatch.setattr(settings, "feishu_document_acl_mode", mode)
    monkeypatch.setattr(settings, "feishu_document_acl_permission", permission)
    monkeypatch.setattr(settings, "feishu_document_acl_fallback_enabled", fallback)
    monkeypatch.setattr(settings, "feishu_document_acl_admin_open_ids", "")


def test_admin_open_id_gets_full_access_while_chat_keeps_view(monkeypatch):
    _set(monkeypatch, mode="CHAT_SCOPE", permission="view")
    monkeypatch.setattr(settings, "feishu_document_acl_admin_open_ids", "ou-admin-1")
    adapter = FakeAdapter(chat_members=["ou-member-a"])
    db = _db()
    case = _source(db)
    service = FeishuDocumentAclService(adapter=adapter)
    asyncio.run(service.reconcile(db, case_id=case.id, document_id="doc-1"))
    perms = {c.member_id: c.perm for c in adapter.collaborators}
    assert perms.get("oc-case") == "view"            # chat members keep view
    assert perms.get("ou-admin-1") == "full_access"  # admin holds manage
    assert ("add", "openid", "ou-admin-1", "full_access") in adapter.calls


def test_chat_scope_adds_group_once_and_second_reconcile_is_idempotent(monkeypatch):
    _set(monkeypatch, "CHAT_SCOPE")
    adapter = FakeAdapter()
    with _db() as db:
        case = _source(db)
        service = FeishuDocumentAclService(adapter=adapter)
        row = asyncio.run(service.reconcile(db, case_id=case.id, document_id="doc-1"))
        assert row.status == "SYNCED"
        assert row.effective_mode == "CHAT_SCOPE"
        assert adapter.calls == [("add", "openchat", "oc-case", "view")]
        asyncio.run(service.reconcile(db, case_id=case.id, document_id="doc-1"))
        assert len(adapter.calls) == 1


def test_auto_falls_back_to_member_mirror_when_group_collaborator_is_unsupported(monkeypatch):
    _set(monkeypatch, "AUTO", fallback=True)
    adapter = FakeAdapter(chat_supported=False, chat_members=["ou-a", "ou-b"])
    with _db() as db:
        case = _source(db)
        row = asyncio.run(FeishuDocumentAclService(adapter=adapter).reconcile(
            db, case_id=case.id, document_id="doc-2"
        ))
        assert row.status == "SYNCED"
        assert row.effective_mode == "MEMBER_MIRROR"
        assert {call[2] for call in adapter.calls if call[0] == "add" and call[1] == "openid"} == {"ou-a", "ou-b"}
        assert row.metadata_json["fallback_from"] == "CHAT_SCOPE"


def test_member_mirror_revokes_user_who_left_group(monkeypatch):
    _set(monkeypatch, "MEMBER_MIRROR")
    adapter = FakeAdapter(
        chat_members=["ou-stay"],
        collaborators=[
            Collaborator("openid", "ou-stay", "view"),
            Collaborator("openid", "ou-left", "view"),
        ],
    )
    with _db() as db:
        case = _source(db)
        asyncio.run(FeishuDocumentAclService(adapter=adapter).reconcile(
            db, case_id=case.id, document_id="doc-3"
        ))
        assert ("remove", "openid", "ou-left", None) in adapter.calls
        assert not any(item.member_id == "ou-left" for item in adapter.collaborators)


def test_permission_change_bumps_revision_and_updates_collaborator(monkeypatch):
    _set(monkeypatch, "CHAT_SCOPE", "view")
    adapter = FakeAdapter(collaborators=[Collaborator("openchat", "oc-case", "view")])
    with _db() as db:
        case = _source(db)
        service = FeishuDocumentAclService(adapter=adapter)
        first = asyncio.run(service.reconcile(db, case_id=case.id, document_id="doc-4"))
        assert first.applied_revision == 1
        monkeypatch.setattr(settings, "feishu_document_acl_permission", "edit")
        second = asyncio.run(service.reconcile(db, case_id=case.id, document_id="doc-4"))
        assert second.desired_revision == 2
        assert second.applied_revision == 2
        assert ("update", "openchat", "oc-case", "edit") in adapter.calls


def test_failure_is_persisted_as_failed_but_canonical_case_remains(monkeypatch):
    _set(monkeypatch, "CHAT_SCOPE", fallback=False)
    adapter = FakeAdapter(chat_supported=False)
    with _db() as db:
        case = _source(db)
        service = FeishuDocumentAclService(adapter=adapter)
        with pytest.raises(FeishuTransportError):
            asyncio.run(service.reconcile(db, case_id=case.id, document_id="doc-5"))
        row = db.scalar(select(FeishuDocumentAclBinding).where(
            FeishuDocumentAclBinding.case_id == case.id
        ))
        assert row is not None
        assert row.status == "FAILED"
        assert db.get(Case, case.id) is not None
