from __future__ import annotations

import json
from pathlib import Path

from app.integrations.feishu.intake import route_intake
from app.integrations.feishu.semantic_router import needs_semantic_fallback


def test_ai1_semantic_router_contract_corpus_gate():
    path = Path(__file__).parent / "fixtures" / "ai1_semantic_router_corpus_v1.json"
    corpus = json.loads(path.read_text(encoding="utf-8"))
    assert corpus["schema_version"] == "ai1-semantic-router-corpus-v1"
    wrong_intent = []
    wrong_fallback = []
    dangerous_false_ai = []
    for item in corpus["cases"]:
        intake = route_intake(
            text=item["text"],
            attachments=item.get("attachments") or [],
            has_thread_case=bool(item.get("has_thread_case")),
        )
        fallback = needs_semantic_fallback(text=item["text"], deterministic=intake)
        if intake.intent != item["expected_intent"]:
            wrong_intent.append((item["id"], intake.intent, item["expected_intent"]))
        if fallback != item["semantic_fallback"]:
            wrong_fallback.append((item["id"], fallback, item["semantic_fallback"]))
        if item.get("dangerous_control") and fallback:
            dangerous_false_ai.append(item["id"])
    assert wrong_intent == []
    assert wrong_fallback == []
    assert dangerous_false_ai == []
