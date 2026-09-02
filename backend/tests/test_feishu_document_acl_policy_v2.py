from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.core.config import settings
from app.integrations.feishu.document_acl import (
    Collaborator,
    FeishuDocumentAclService,
)


class FakePermissionAdapter:
    def __init__(self, *, collaborators=None, chat_members=None, bot_in_chat=True):
        self.collaborators = {
            (item.member_type, item.member_id): item
            for item in (collaborators or [])
        }
        self.chat_members = list(chat_members or [])
        self.is_bot_in_chat = bot_in_chat
        self.calls: list[tuple] = []

    async def list_collaborators(self, document_id: str):
        return list(self.collaborators.values())

    async def add_collaborator(self, document_id: str, *, member_type: str, member_id: str, perm: str):
        self.calls.append(("add", member_type, member_id, perm))
        self.collaborators[(member_type, member_id)] = Collaborator(member_type, member_id, perm)

    async def update_collaborator(self, document_id: str, *, member_type: str, member_id: str, perm: str):
        self.calls.append(("update", member_type, member_id, perm))
        self.collaborators[(member_type, member_id)] = Collaborator(member_type, member_id, perm)

    async def remove_collaborator(self, document_id: str, *, member_type: str, member_id: str):
        self.calls.append(("remove", member_type, member_id))
        self.collaborators.pop((member_type, member_id), None)

    async def list_chat_members(self, chat_id: str):
        return list(self.chat_members)

    async def bot_in_chat(self, chat_id: str):
        return self.is_bot_in_chat


def _row(*, metadata=None, effective_mode=None):
    return SimpleNamespace(
        document_id="doc-1",
        chat_id="chat-1",
        metadata_json=dict(metadata or {}),
        effective_mode=effective_mode,
    )


def _perm(adapter: FakePermissionAdapter, member_type: str, member_id: str):
    item = adapter.collaborators.get((member_type, member_id))
    return item.perm if item else None


def test_chat_scope_source_chat_and_initiator_are_view_admin_is_full_access():
    adapter = FakePermissionAdapter()
    service = FeishuDocumentAclService(adapter=adapter)

    result = asyncio.run(service._chat_scope(
        _row(),
        initiator_open_id="ou_initiator",
        admin_open_ids=["ou_admin"],
    ))

    assert result["mode"] == "CHAT_SCOPE"
    assert _perm(adapter, "openchat", "chat-1") == "view"
    assert _perm(adapter, "openid", "ou_initiator") == "view"
    assert _perm(adapter, "openid", "ou_admin") == "full_access"


def test_admin_precedence_prevents_initiator_downgrade_to_view():
    adapter = FakePermissionAdapter()
    service = FeishuDocumentAclService(adapter=adapter)

    asyncio.run(service._chat_scope(
        _row(),
        initiator_open_id="ou_same",
        admin_open_ids=["ou_same"],
    ))

    assert _perm(adapter, "openid", "ou_same") == "full_access"
    assert ("add", "openid", "ou_same", "view") not in adapter.calls


def test_member_mirror_group_and_initiator_are_view_admins_are_full_access_without_churn():
    adapter = FakePermissionAdapter(
        collaborators=[
            Collaborator("openid", "ou_member", "full_access"),
            Collaborator("openid", "ou_admin", "full_access"),
        ],
        chat_members=["ou_member", "ou_admin"],
    )
    service = FeishuDocumentAclService(adapter=adapter)

    result = asyncio.run(service._member_mirror(
        _row(),
        initiator_open_id="ou_initiator",
        admin_open_ids=["ou_admin", "ou_external_admin"],
    ))

    assert result["mode"] == "MEMBER_MIRROR"
    assert _perm(adapter, "openid", "ou_member") == "view"
    assert _perm(adapter, "openid", "ou_initiator") == "view"
    assert _perm(adapter, "openid", "ou_admin") == "full_access"
    assert _perm(adapter, "openid", "ou_external_admin") == "full_access"
    # An admin already at full_access must not be temporarily downgraded to view.
    assert ("update", "openid", "ou_admin", "view") not in adapter.calls


def test_removed_admin_is_downgraded_to_view_when_it_is_case_initiator():
    adapter = FakePermissionAdapter(collaborators=[
        Collaborator("openchat", "chat-1", "view"),
        Collaborator("openid", "ou_old_admin", "full_access"),
    ])
    service = FeishuDocumentAclService(adapter=adapter)
    row = _row(metadata={
        "managed_admin_open_ids": ["ou_old_admin"],
        "managed_initiator_open_id": "ou_old_admin",
    })

    asyncio.run(service._chat_scope(
        row,
        initiator_open_id="ou_old_admin",
        admin_open_ids=[],
    ))

    assert _perm(adapter, "openid", "ou_old_admin") == "view"


def test_removed_admin_direct_grant_is_removed_when_no_longer_managed():
    adapter = FakePermissionAdapter(collaborators=[
        Collaborator("openchat", "chat-1", "view"),
        Collaborator("openid", "ou_old_admin", "full_access"),
    ])
    service = FeishuDocumentAclService(adapter=adapter)
    row = _row(metadata={
        "managed_admin_open_ids": ["ou_old_admin"],
        "managed_initiator_open_id": None,
    })

    asyncio.run(service._chat_scope(
        row,
        initiator_open_id=None,
        admin_open_ids=[],
    ))

    assert _perm(adapter, "openid", "ou_old_admin") is None
    assert ("remove", "openid", "ou_old_admin") in adapter.calls


class FakeBindingDb:
    def __init__(self, source, row):
        self.source = source
        self.row = row
        self.scalar_calls = 0
        self.flush_calls = 0

    def scalar(self, _statement):
        self.scalar_calls += 1
        return self.source if self.scalar_calls == 1 else self.row

    def add(self, _row):
        raise AssertionError("existing-row test must not add a binding")

    def flush(self):
        self.flush_calls += 1


def _source():
    return SimpleNamespace(
        source_tenant_key="tenant-1",
        receive_id="chat-1",
        source_sender_open_id="ou_initiator",
    )


def _binding(*, fingerprint: str, revision: int = 7):
    return SimpleNamespace(
        tenant_key="tenant-1",
        chat_id="chat-1",
        sync_mode="AUTO",
        desired_permission="view",
        desired_revision=revision,
        applied_revision=revision,
        status="SYNCED",
        metadata_json={"desired_acl_fingerprint": fingerprint},
    )


def test_admin_list_change_invalidates_synced_acl_revision(monkeypatch):
    monkeypatch.setattr(settings, "feishu_document_acl_mode", "AUTO")
    monkeypatch.setattr(settings, "feishu_document_acl_fallback_enabled", True)
    monkeypatch.setattr(settings, "feishu_document_acl_admin_open_ids", "ou_admin_a")
    source = _source()
    old_fingerprint = FeishuDocumentAclService._desired_acl_fingerprint(source, "AUTO")
    row = _binding(fingerprint=old_fingerprint)

    monkeypatch.setattr(settings, "feishu_document_acl_admin_open_ids", "ou_admin_b")
    db = FakeBindingDb(source, row)
    resolved = FeishuDocumentAclService.ensure_binding(db, case_id="case-1", document_id="doc-1")

    assert resolved is row
    assert row.desired_revision == 8
    assert row.status == "PENDING"
    assert row.metadata_json["desired_acl_fingerprint"] != old_fingerprint


def test_same_acl_policy_is_idempotent_and_normal_permission_is_fixed_view(monkeypatch):
    monkeypatch.setattr(settings, "feishu_document_acl_mode", "AUTO")
    monkeypatch.setattr(settings, "feishu_document_acl_fallback_enabled", True)
    monkeypatch.setattr(settings, "feishu_document_acl_admin_open_ids", "ou_admin")
    # Even a stale legacy setting must not elevate normal Case/chat principals.
    monkeypatch.setattr(settings, "feishu_document_acl_permission", "full_access")
    source = _source()
    fingerprint = FeishuDocumentAclService._desired_acl_fingerprint(source, "AUTO")
    row = _binding(fingerprint=fingerprint)

    db = FakeBindingDb(source, row)
    FeishuDocumentAclService.ensure_binding(db, case_id="case-1", document_id="doc-1")

    assert row.desired_revision == 7
    assert row.status == "SYNCED"
    assert row.desired_permission == "view"
