from __future__ import annotations

from app.conversation.interpreter import ConversationInterpreter, deterministic_interpret_turn
from app.integrations.feishu.intake import route_intake


def _interpret(text: str, *, active_question=None, has_case=True):
    intake = route_intake(text=text, attachments=[], has_thread_case=has_case)
    return deterministic_interpret_turn(
        text=text,
        attachments=[],
        deterministic=intake,
        active_question=active_question,
        has_case=has_case,
    )


def test_unknown_active_timestamp_is_chat_only_and_not_evidence():
    result = _interpret(
        "不知道",
        active_question={"id": "q1", "slot_key": "anomaly_timestamp", "text": "异常时间？"},
    )
    assert result["intent"] == "ANSWER_ACTIVE_QUESTION"
    assert result["classification"] == "CHAT_ONLY"
    assert result["material_diagnostic_context"] is False
    assert result["active_question_answer"]["state"] == "UNKNOWN_BY_USER"
    assert result["active_question_answer"]["slot_key"] == "anomaly_timestamp"


def test_0817_answers_active_timestamp_and_becomes_material_context():
    result = _interpret(
        "0817",
        active_question={"id": "q1", "slot_key": "anomaly_timestamp", "text": "异常时间？"},
    )
    assert result["intent"] == "ANSWER_ACTIVE_QUESTION"
    assert result["classification"] == "DIAGNOSTIC_CONTEXT"
    assert result["material_diagnostic_context"] is True
    assert result["active_question_answer"]["value"] == "08:17"


def test_when_can_analysis_finish_is_status_not_new_diagnosis():
    intake = route_intake(text="什么时候可以结束分析", attachments=[], has_thread_case=True)
    assert intake.intent == "STATUS_QUERY"
    result = _interpret("什么时候可以结束分析")
    assert result["intent"] == "CASE_COMPLETION_QUERY"
    assert result["classification"] == "CHAT_ONLY"
    assert result["material_diagnostic_context"] is False


def test_next_action_question_is_chat_only():
    result = _interpret("还需要我做什么？")
    assert result["intent"] == "CASE_NEXT_ACTION_QUERY"
    assert result["classification"] == "CHAT_ONLY"
    assert result["material_diagnostic_context"] is False


def test_legacy_unknown_without_active_question_never_consumes_diagnosis_cycle():
    result = _interpret("不知道", active_question=None)
    assert result["intent"] == "CASE_CHAT"
    assert result["classification"] == "CHAT_ONLY"
    assert result["material_diagnostic_context"] is False


def test_legacy_unavailable_without_active_question_never_becomes_evidence():
    result = _interpret("暂时不能", active_question=None)
    assert result["classification"] == "CHAT_ONLY"
    assert result["material_diagnostic_context"] is False


def test_case_knowledge_question_is_non_material():
    result = _interpret("RFC2833 是什么？", active_question=None, has_case=True)
    assert result["intent"] == "KNOWLEDGE_IN_CASE"
    assert result["classification"] == "KNOWLEDGE"
    assert result["material_diagnostic_context"] is False


def test_substantive_case_follow_up_is_material():
    result = _interpret("换回原装话柄后声音正常了", active_question=None, has_case=True)
    assert result["classification"] == "DIAGNOSTIC_CONTEXT"
    assert result["material_diagnostic_context"] is True


class FakeGateway:
    def enabled(self):
        return True

    def interpret_turn(self, **kwargs):
        return {
            "proposal": {
                "schema_version": "conversation-turn-v1",
                "intent": "DIAGNOSTIC_CONTEXT",
                "classification": "DIAGNOSTIC_CONTEXT",
                "route_mode": "DIAGNOSIS_FOLLOW_UP",
                "active_question_answer": None,
                "entities": {"invented": "context"},
                "material_diagnostic_context": True,
                "needs_clarification": False,
                "clarification_question": None,
                "confidence": 0.99,
                "safety_class": "NON_EXECUTING_SEMANTIC_PROPOSAL",
            },
            "model": "fake",
            "prompt_version": "test",
        }


def test_ai_cannot_upgrade_progress_chat_into_evidence(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "conversation_ai_enabled", True)
    monkeypatch.setattr(settings, "conversation_ai_mode", "ON")
    monkeypatch.setattr(settings, "conversation_ai_min_confidence", 0.8)
    intake = route_intake(text="进度怎么样？", attachments=[], has_thread_case=True)
    result = ConversationInterpreter(gateway=FakeGateway()).interpret(
        text="进度怎么样？",
        attachments=[],
        deterministic=intake,
        active_question=None,
        slots={},
        case_context={"case_id": "c1", "case_no": "CASE-1", "status": "ANALYZING"},
    )
    assert result.proposal["intent"] == "CASE_PROGRESS_QUERY"
    assert result.proposal["material_diagnostic_context"] is False
    assert result.llm_status == "ACTIVE_REJECTED"
