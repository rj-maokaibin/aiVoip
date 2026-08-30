from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.conversation.interpreter import deterministic_interpret_turn
from app.conversation.response import GroundedConversationResponder
from app.conversation.snapshot import ConversationSnapshotBuilder
from app.conversation.state_service import ConversationStateService
from app.integrations.feishu.intake import route_intake


@pytest.mark.parametrize(
    "text",
    [
        "现在进展怎么样？",
        "当前进展如何？",
        "进展到哪了？",
        "现在什么进展？",
    ],
)
def test_progress_synonyms_route_to_current_case_progress(text: str):
    preliminary = route_intake(text=text, attachments=[], has_thread_case=False)
    assert preliminary.intent == "STATUS_QUERY"
    assert preliminary.reason == "status_or_completion_phrase"
    assert preliminary.intent != "GENERAL_QUESTION"

    correlated = route_intake(text=text, attachments=[], has_thread_case=True)
    assert correlated.intent == "STATUS_QUERY"
    assert correlated.missing_user_inputs == []

    proposal = deterministic_interpret_turn(
        text=text,
        attachments=[],
        deterministic=correlated,
        active_question={"slot_key": "anomaly_timestamp"},
        has_case=True,
    )
    assert proposal["intent"] == "CASE_PROGRESS_QUERY"
    assert proposal["classification"] == "CHAT_ONLY"
    assert proposal["route_mode"] == "CASE_CHAT"
    assert proposal["material_diagnostic_context"] is False
    assert proposal["intent"] != "KNOWLEDGE_IN_CASE"


class _ScalarSequenceDb:
    def __init__(self, case, diagnosis):
        self.case = case
        self.values = iter([diagnosis, None])

    def get(self, _model, _case_id):
        return self.case

    def scalar(self, _query):
        return next(self.values)

    def scalars(self, _query):
        return []


def test_partial_candidate_without_explicit_unknown_still_surfaces_unresolved_boundary(monkeypatch):
    case = SimpleNamespace(
        id="case-p2-progress",
        case_no="VOIP-20260830-5F8EA9",
        status="WAITING_USER",
    )
    diagnosis = SimpleNamespace(
        case_id=case.id,
        status="ANALYZING",
        cycle=1,
        summary_json={
            "headline": "候选方向，需补证：pcm_tx 与对应RTP媒体路径内容高度一致",
            "known": [
                "SIP呼叫 2 通，其中已建立/正常结束 1 通",
                "识别到 3 路RTP媒体流",
            ],
            "unknown": [],
            "blocking_reason": None,
        },
        decision_json={},
    )
    db = _ScalarSequenceDb(case, diagnosis)
    monkeypatch.setattr(
        ConversationStateService,
        "case_state",
        lambda self, _db, _case_id: (None, None),
    )

    snapshot = ConversationSnapshotBuilder().build(db, case.id)
    assert snapshot["diagnosis"]["unknown"] == ["当前证据仍不足以确认最终根因"]
    assert snapshot["uncertainty_catalog"]["unknown_1"] == "当前证据仍不足以确认最终根因"

    responder = object.__new__(GroundedConversationResponder)
    text = responder._deterministic_render(snapshot, "CASE_PROGRESS_QUERY", {})
    assert "已确认" in text
    assert "尚未确认" in text
    assert "当前证据仍不足以确认最终根因" in text
    assert "知识库中没有找到足够匹配的答案" not in text
