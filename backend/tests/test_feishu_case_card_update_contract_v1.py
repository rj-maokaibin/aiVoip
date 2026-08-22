from __future__ import annotations

from types import SimpleNamespace

import pytest

import app.integrations.feishu.service as service_module
from app.integrations.feishu.service import FeishuCaseCardService


class FakeDb:
    def __init__(self, binding):
        self.binding = binding
        self.flush_count = 0

    def scalar(self, _stmt):
        return self.binding

    def flush(self):
        self.flush_count += 1


class FakeBuilder:
    def build(self, _db, case_id):
        return SimpleNamespace(card={"type": "template", "data": {"case_id": case_id}})


class FakeTransport:
    sent = []
    updated = []

    async def send_card(self, *, receive_id, receive_id_type, card):
        self.__class__.sent.append((receive_id, receive_id_type, card))
        return SimpleNamespace(message_id="msg-001")

    async def update_card(self, *, message_id, card):
        self.__class__.updated.append((message_id, card))
        return None


@pytest.mark.asyncio
async def test_fr022_existing_case_card_updates_same_message_without_second_send(monkeypatch):
    monkeypatch.setattr(service_module.settings, "feishu_live_enabled", True)
    monkeypatch.setattr(service_module, "FeishuCaseCardBuilder", FakeBuilder)
    monkeypatch.setattr(service_module, "FeishuLiveTransport", FakeTransport)
    FakeTransport.sent = []
    FakeTransport.updated = []

    binding = SimpleNamespace(
        case_id="case-1", receive_id="chat-1", receive_id_type="chat_id",
        message_id="msg-existing", status="ACTIVE", card_version=3,
    )
    db = FakeDb(binding)

    result = await FeishuCaseCardService().sync_case_card(db, case_id="case-1")

    assert result is binding
    assert FakeTransport.sent == []
    assert [row[0] for row in FakeTransport.updated] == ["msg-existing"]
    assert binding.message_id == "msg-existing"
    assert binding.card_version == 4


@pytest.mark.asyncio
async def test_fr022_first_case_card_send_persists_message_id_for_future_updates(monkeypatch):
    monkeypatch.setattr(service_module.settings, "feishu_live_enabled", True)
    monkeypatch.setattr(service_module, "FeishuCaseCardBuilder", FakeBuilder)
    monkeypatch.setattr(service_module, "FeishuLiveTransport", FakeTransport)
    FakeTransport.sent = []
    FakeTransport.updated = []

    binding = SimpleNamespace(
        case_id="case-1", receive_id="chat-1", receive_id_type="chat_id",
        message_id=None, status="ACTIVE", card_version=0,
    )
    db = FakeDb(binding)

    result = await FeishuCaseCardService().sync_case_card(db, case_id="case-1")

    assert len(FakeTransport.sent) == 1
    assert FakeTransport.updated == []
    assert result.message_id == "msg-001"
    assert result.card_version == 1
