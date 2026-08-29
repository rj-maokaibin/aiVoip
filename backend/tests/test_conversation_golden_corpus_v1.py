from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.conversation.interpreter import deterministic_interpret_turn
from app.integrations.feishu.intake import route_intake


CORPUS = json.loads(
    (Path(__file__).parent / "fixtures" / "conversation_p0_p1_corpus_v1.json").read_text(encoding="utf-8")
)


@pytest.mark.parametrize("item", CORPUS["cases"], ids=lambda item: item["id"])
def test_conversation_p0_p1_golden_corpus(item):
    text = item["text"]
    has_case = bool(item.get("has_case"))
    deterministic = route_intake(text=text, attachments=[], has_thread_case=has_case)
    proposal = deterministic_interpret_turn(
        text=text,
        attachments=[],
        deterministic=deterministic,
        active_question=item.get("active_question"),
        has_case=has_case,
    )

    assert proposal["intent"] == item["expected_intent"]
    assert proposal["classification"] == item["expected_classification"]
    assert bool(proposal["material_diagnostic_context"]) is bool(item["expected_material"])

    if item.get("expected_slot_state"):
        answer = proposal.get("active_question_answer") or {}
        assert answer.get("state") == item["expected_slot_state"]
    if "expected_slot_value" in item:
        answer = proposal.get("active_question_answer") or {}
        assert answer.get("value") == item["expected_slot_value"]
    if item.get("expected_control"):
        assert (proposal.get("entities") or {}).get("control") == item["expected_control"]


def test_corpus_contains_required_humanization_risks():
    ids = {item["id"] for item in CORPUS["cases"]}
    assert {
        "timestamp-unknown",
        "timestamp-short-answer",
        "completion-query",
        "knowledge-inside-active-question",
        "hybrid-knowledge-diagnosis",
        "continue-control",
        "finish-partial-control",
    } <= ids
