from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.conversation.entities import resolve_catalog_entities
from app.conversation.interpreter import ConversationInterpreter, InterpretationResult
from app.conversation.state_service import ConversationStateService
from app.integrations.feishu.intake import route_intake
from app.knowledge.conversation_service import answer_grounded_knowledge


@dataclass(frozen=True)
class OrchestratedTurn:
    conversation_id: str
    turn_id: str
    interpretation: dict[str, Any]
    llm_status: str
    material_diagnostic_context: bool
    route_mode: str
    response_text: str | None


class AssistantConversationOrchestrator:
    """Unified non-executing Conversation Platform entry point.

    Responsibilities are deliberately limited to conversation semantics:
      * resolve/create Conversation + persistent context
      * interpret one user turn
      * persist ConversationTurn and safe entities
      * answer knowledge-only turns from controlled knowledge authorities

    It never creates technical Evidence, never resumes Diagnosis, and never invokes
    SSH/reproduction/device actions. A caller that sees
    ``material_diagnostic_context=True`` must cross the existing deterministic
    Evidence/Diagnosis bridge explicitly.
    """

    def __init__(self, interpreter: ConversationInterpreter | None = None):
        self.state = ConversationStateService()
        self.interpreter = interpreter or ConversationInterpreter()

    def prepare_turn(
        self,
        db: Session,
        *,
        text: str,
        source_context: dict[str, Any],
        case_id: str | None = None,
        case_context: dict[str, Any] | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> OrchestratedTurn:
        attachments = list(attachments or [])
        conversation, state = self.state.get_or_create(
            db, case_id=case_id, source_context=source_context
        )
        existing_entities = dict(conversation.entities_json or {})
        inherited_entities = resolve_catalog_entities(db, text, existing_entities)

        deterministic = route_intake(
            text=text,
            attachments=attachments,
            has_thread_case=bool(case_id),
        )
        interpreted: InterpretationResult = self.interpreter.interpret(
            text=text,
            attachments=attachments,
            deterministic=deterministic,
            active_question=state.active_question_json,
            slots=state.slots_json or {},
            case_context=case_context,
        )
        parsed = dict(interpreted.proposal)
        parsed["llm_status"] = interpreted.llm_status
        parsed_entities = dict(parsed.get("entities") or {})
        merged_entities = {**inherited_entities, **parsed_entities}
        parsed["entities"] = merged_entities
        if interpreted.ai_proposal is not None:
            parsed["ai_shadow_proposal"] = interpreted.ai_proposal

        turn = self.state.record_user_turn(
            db,
            case_id=case_id,
            source_context=source_context,
            text=text,
            interpretation=parsed,
            model_name=interpreted.model_name,
            prompt_version=interpreted.prompt_version,
        )

        response_text = None
        intent = str(parsed.get("intent") or "")
        if not case_id and intent in {"KNOWLEDGE_QUERY", "GENERAL_CHAT"}:
            if intent == "KNOWLEDGE_QUERY":
                answer = answer_grounded_knowledge(db, text, entities=merged_entities)
                response_text = str(answer.get("text") or "")
            else:
                response_text = "可以继续问产品规格、协议或配置问题；如果是在排查现场故障，请直接描述具体现象。"

        return OrchestratedTurn(
            conversation_id=conversation.id,
            turn_id=turn.id,
            interpretation=parsed,
            llm_status=interpreted.llm_status,
            material_diagnostic_context=bool(parsed.get("material_diagnostic_context")),
            route_mode=str(parsed.get("route_mode") or ""),
            response_text=response_text,
        )
