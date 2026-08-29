from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from pydantic import ValidationError

from app.conversation.contracts import ConversationTurnProposal
from app.conversation.gateway import ConversationGatewayClient, ConversationGatewayError
from app.core.config import settings
from app.integrations.feishu.intake import IntakeResult

_PROGRESS = re.compile(r"(?:进度|状态|到哪(?:了)?|分析到哪|结果(?:出来|有了|了吗)|现在怎么样|什么情况)", re.I)
_COMPLETION = re.compile(r"(?:什么时候.*(?:结束|完成)|还要多久|多久.*(?:结束|完成)|分析.*(?:结束|完成)(?:了吗|了没|没有)?|可以结束(?:分析|诊断)?吗|能结束(?:分析|诊断)?吗)", re.I)
_NEXT_ACTION = re.compile(r"(?:还需要我做什么|需要我做什么|我还要做什么|下一步(?:做什么|怎么办)?|还缺什么|还需要什么|需要补充什么)", re.I)
_UNKNOWN = re.compile(r"^(?:不知道|不清楚|不确定|记不住|想不起来|忘了|未知)[。.!！ ]*$", re.I)
_UNAVAILABLE = re.compile(r"^(?:暂时不能|现在不能|目前不能|没法|无法|暂时没法|做不了|不能复现|没法复现)[。.!！ ]*$", re.I)
_NONE = re.compile(r"^(?:没有|没(?:有)?|无)[。.!！ ]*$", re.I)
_CONTINUE = re.compile(r"^(?:继续|继续分析|继续吧|往下分析|好的继续|好继续)[。.!！ ]*$", re.I)
_ACK = re.compile(r"^(?:好的?|行|可以|收到|明白|知道了|谢谢|感谢|ok|okay)[。.!！ ]*$", re.I)
_STOP = re.compile(r"^(?:结束吧|结束分析|结束诊断|按现有证据出结论|按现有结果出结论|给阶段结论)[。.!！ ]*$", re.I)
_TIME_4 = re.compile(r"^(?:[01]?\d|2[0-3])(?:[0-5]\d)$")
_TIME_COLON = re.compile(r"^(?:[01]?\d|2[0-3]):[0-5]\d$")


@dataclass(frozen=True)
class InterpretationResult:
    proposal: dict[str, Any]
    llm_status: str
    model_name: str | None = None
    prompt_version: str | None = None
    deterministic_proposal: dict[str, Any] | None = None
    ai_proposal: dict[str, Any] | None = None


def _proposal(
    *,
    intent: str,
    classification: str,
    route_mode: str,
    confidence: float,
    active_question_answer: dict[str, Any] | None = None,
    entities: dict[str, Any] | None = None,
    material: bool = False,
    needs_clarification: bool = False,
    clarification_question: str | None = None,
) -> dict[str, Any]:
    return ConversationTurnProposal(
        schema_version="conversation-turn-v1",
        intent=intent,
        classification=classification,
        route_mode=route_mode,
        active_question_answer=active_question_answer,
        entities=entities or {},
        material_diagnostic_context=material,
        needs_clarification=needs_clarification,
        clarification_question=clarification_question,
        confidence=confidence,
        safety_class="NON_EXECUTING_SEMANTIC_PROPOSAL",
    ).model_dump(mode="json")


def _normalize_hhmm(text: str) -> str | None:
    raw = (text or "").strip()
    if _TIME_COLON.fullmatch(raw):
        hour, minute = raw.split(":", 1)
        return f"{int(hour):02d}:{int(minute):02d}"
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 3 and digits.isdigit():
        digits = f"0{digits}"
    if len(digits) == 4 and _TIME_4.fullmatch(digits):
        return f"{int(digits[:2]):02d}:{int(digits[2:]):02d}"
    return None


def deterministic_interpret_turn(
    *,
    text: str,
    attachments: list[dict[str, Any]],
    deterministic: IntakeResult,
    active_question: dict[str, Any] | None,
    has_case: bool,
) -> dict[str, Any]:
    """Fail-closed local interpretation for P0 conversational correctness.

    This intentionally handles only high-confidence interaction semantics. Novel
    language may be proposed by the optional LLM path but cannot bypass the
    material-evidence boundary.
    """
    normalized = (text or "").strip()
    if attachments:
        return _proposal(
            intent="ATTACHMENT",
            classification="ATTACHMENT",
            route_mode="ATTACHMENT",
            confidence=0.99,
            material=True,
        )

    if _COMPLETION.search(normalized):
        return _proposal(
            intent="CASE_COMPLETION_QUERY",
            classification="CHAT_ONLY",
            route_mode="CASE_CHAT",
            confidence=0.99,
        )
    if _NEXT_ACTION.search(normalized):
        return _proposal(
            intent="CASE_NEXT_ACTION_QUERY",
            classification="CHAT_ONLY",
            route_mode="CASE_CHAT",
            confidence=0.99,
        )
    if _PROGRESS.search(normalized) or deterministic.intent == "STATUS_QUERY":
        return _proposal(
            intent="CASE_PROGRESS_QUERY",
            classification="CHAT_ONLY",
            route_mode="CASE_CHAT",
            confidence=0.98,
        )

    active_question = dict(active_question or {})
    slot_key = str(active_question.get("slot_key") or "")
    if slot_key:
        if _UNKNOWN.fullmatch(normalized):
            return _proposal(
                intent="ANSWER_ACTIVE_QUESTION",
                classification="CHAT_ONLY",
                route_mode="CASE_CHAT",
                confidence=0.99,
                active_question_answer={
                    "slot_key": slot_key,
                    "state": "UNKNOWN_BY_USER",
                    "value": None,
                    "confidence": 0.99,
                },
            )
        if _UNAVAILABLE.fullmatch(normalized) or (_NONE.fullmatch(normalized) and slot_key in {"recording", "pcap", "device_access"}):
            return _proposal(
                intent="ANSWER_ACTIVE_QUESTION",
                classification="CHAT_ONLY",
                route_mode="CASE_CHAT",
                confidence=0.99,
                active_question_answer={
                    "slot_key": slot_key,
                    "state": "UNAVAILABLE",
                    "value": None,
                    "confidence": 0.99,
                },
            )
        if slot_key == "anomaly_timestamp":
            value = _normalize_hhmm(normalized)
            if value:
                return _proposal(
                    intent="ANSWER_ACTIVE_QUESTION",
                    classification="DIAGNOSTIC_CONTEXT",
                    route_mode="DIAGNOSIS_FOLLOW_UP",
                    confidence=0.98,
                    active_question_answer={
                        "slot_key": slot_key,
                        "state": "ANSWERED",
                        "value": value,
                        "confidence": 0.98,
                    },
                    entities={"anomaly_timestamp": value},
                    material=True,
                )
        if slot_key == "reproducibility":
            if normalized in {"可以", "能", "可以复现", "能复现", "是"}:
                return _proposal(
                    intent="ANSWER_ACTIVE_QUESTION",
                    classification="DIAGNOSTIC_CONTEXT",
                    route_mode="DIAGNOSIS_FOLLOW_UP",
                    confidence=0.98,
                    active_question_answer={
                        "slot_key": slot_key,
                        "state": "ANSWERED",
                        "value": "YES",
                        "confidence": 0.98,
                    },
                    entities={"reproducibility": "YES"},
                    material=True,
                )
            if normalized in {"不确定", "不一定"}:
                return _proposal(
                    intent="ANSWER_ACTIVE_QUESTION",
                    classification="CHAT_ONLY",
                    route_mode="CASE_CHAT",
                    confidence=0.98,
                    active_question_answer={
                        "slot_key": slot_key,
                        "state": "UNKNOWN_BY_USER",
                        "value": "UNCERTAIN",
                        "confidence": 0.98,
                    },
                )
        # A concise answer while a question is active is diagnostic by default;
        # keep the model optional rather than discarding potentially useful field context.
        if normalized:
            return _proposal(
                intent="ANSWER_ACTIVE_QUESTION",
                classification="DIAGNOSTIC_CONTEXT",
                route_mode="DIAGNOSIS_FOLLOW_UP",
                confidence=0.82,
                active_question_answer={
                    "slot_key": slot_key,
                    "state": "ANSWERED",
                    "value": normalized[:500],
                    "confidence": 0.82,
                },
                material=True,
            )

    # Upgrade compatibility: a Case may already be WAITING_USER from the previous
    # release without a persisted active_question. Standalone inability answers are
    # therefore fail-safe chat constraints, never new L1 technical Evidence.
    if has_case and _UNKNOWN.fullmatch(normalized):
        return _proposal(
            intent="CASE_CHAT",
            classification="CHAT_ONLY",
            route_mode="CASE_CHAT",
            confidence=0.97,
            entities={"legacy_unresolved_answer": "UNKNOWN_BY_USER"},
        )
    if has_case and (_UNAVAILABLE.fullmatch(normalized) or _NONE.fullmatch(normalized)):
        return _proposal(
            intent="CASE_CHAT",
            classification="CHAT_ONLY",
            route_mode="CASE_CHAT",
            confidence=0.97,
            entities={"legacy_unresolved_answer": "UNAVAILABLE"},
        )

    if _STOP.fullmatch(normalized):
        return _proposal(
            intent="CONTROL",
            classification="CONTROL",
            route_mode="CONTROL",
            confidence=0.99,
            entities={"control": "FINISH_WITH_PARTIAL_CONCLUSION"},
        )
    if _CONTINUE.fullmatch(normalized):
        return _proposal(
            intent="CONTROL",
            classification="CONTROL",
            route_mode="CONTROL",
            confidence=0.97,
            entities={"control": "CONTINUE_ANALYSIS"},
        )
    if _ACK.fullmatch(normalized):
        return _proposal(
            intent="GENERAL_CHAT",
            classification="CHAT_ONLY",
            route_mode="CASE_CHAT" if has_case else "KNOWLEDGE",
            confidence=0.98,
        )

    if deterministic.intent == "GENERAL_QUESTION":
        return _proposal(
            intent="KNOWLEDGE_IN_CASE" if has_case else "KNOWLEDGE_QUERY",
            classification="KNOWLEDGE",
            route_mode="KNOWLEDGE_IN_CASE" if has_case else "KNOWLEDGE",
            confidence=max(0.90, float(deterministic.confidence)),
        )
    if has_case and deterministic.intent == "CASE_FOLLOW_UP":
        return _proposal(
            intent="DIAGNOSTIC_CONTEXT",
            classification="DIAGNOSTIC_CONTEXT",
            route_mode="DIAGNOSIS_FOLLOW_UP",
            confidence=0.86,
            material=True,
        )
    if has_case and deterministic.intent == "NEW_DIAGNOSIS":
        return _proposal(
            intent="DIAGNOSTIC_CONTEXT",
            classification="DIAGNOSTIC_CONTEXT",
            route_mode="DIAGNOSIS_FOLLOW_UP",
            confidence=0.84,
            material=True,
        )
    return _proposal(
        intent="GENERAL_CHAT",
        classification="CHAT_ONLY",
        route_mode="CASE_CHAT" if has_case else "KNOWLEDGE",
        confidence=0.60,
    )


def _ai_can_override(deterministic: dict[str, Any], ai: ConversationTurnProposal) -> bool:
    # P0 safety: AI may make a turn *less* diagnostic, but may not upgrade a
    # deterministic non-material turn into technical Evidence on its own.
    if not deterministic.get("material_diagnostic_context") and ai.material_diagnostic_context:
        return False
    if ai.classification in {"CHAT_ONLY", "KNOWLEDGE", "CONTROL"} and ai.material_diagnostic_context:
        return False
    return True


class ConversationInterpreter:
    def __init__(self, gateway: ConversationGatewayClient | None = None):
        self.gateway = gateway or ConversationGatewayClient()

    def interpret(
        self,
        *,
        text: str,
        attachments: list[dict[str, Any]],
        deterministic: IntakeResult,
        active_question: dict[str, Any] | None,
        slots: dict[str, Any] | None,
        case_context: dict[str, Any] | None,
    ) -> InterpretationResult:
        baseline = deterministic_interpret_turn(
            text=text,
            attachments=attachments,
            deterministic=deterministic,
            active_question=active_question,
            has_case=bool(case_context),
        )
        enabled = bool(settings.conversation_ai_enabled)
        mode = str(settings.conversation_ai_mode or "OFF").upper()
        if not enabled or mode == "OFF" or not self.gateway.enabled():
            return InterpretationResult(
                proposal=baseline,
                llm_status="BYPASSED",
                deterministic_proposal=baseline,
            )
        try:
            raw = self.gateway.interpret_turn(
                text=text,
                attachments=attachments,
                active_question=active_question,
                slots=slots or {},
                case_context=case_context,
                deterministic_candidate=baseline,
            )
            ai = ConversationTurnProposal.model_validate(raw["proposal"])
            ai_payload = ai.model_dump(mode="json")
            if ai.confidence < float(settings.conversation_ai_min_confidence):
                return InterpretationResult(
                    proposal=baseline,
                    llm_status="LOW_CONFIDENCE",
                    deterministic_proposal=baseline,
                    ai_proposal=ai_payload,
                    model_name=raw.get("model"),
                    prompt_version=raw.get("prompt_version"),
                )
            if mode == "ON" and _ai_can_override(baseline, ai):
                return InterpretationResult(
                    proposal=ai_payload,
                    llm_status="ACTIVE_VALID",
                    deterministic_proposal=baseline,
                    ai_proposal=ai_payload,
                    model_name=raw.get("model"),
                    prompt_version=raw.get("prompt_version"),
                )
            return InterpretationResult(
                proposal=baseline,
                llm_status="SHADOW_VALID" if mode == "SHADOW" else "ACTIVE_REJECTED",
                deterministic_proposal=baseline,
                ai_proposal=ai_payload,
                model_name=raw.get("model"),
                prompt_version=raw.get("prompt_version"),
            )
        except (ConversationGatewayError, ValidationError, ValueError, KeyError, TypeError) as exc:
            return InterpretationResult(
                proposal=baseline,
                llm_status=f"FALLBACK:{str(exc).split(':', 1)[0][:96]}",
                deterministic_proposal=baseline,
            )