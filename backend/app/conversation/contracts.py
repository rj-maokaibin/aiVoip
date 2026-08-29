from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

TurnIntent = Literal[
    "CASE_PROGRESS_QUERY",
    "CASE_COMPLETION_QUERY",
    "CASE_NEXT_ACTION_QUERY",
    "ANSWER_ACTIVE_QUESTION",
    "KNOWLEDGE_QUERY",
    "KNOWLEDGE_IN_CASE",
    "HYBRID_KNOWLEDGE_DIAGNOSIS",
    "DIAGNOSTIC_CONTEXT",
    "CASE_CHAT",
    "CONTROL",
    "ATTACHMENT",
    "GENERAL_CHAT",
]
TurnClassification = Literal[
    "CHAT_ONLY", "CONTROL", "DIAGNOSTIC_CONTEXT", "KNOWLEDGE", "ATTACHMENT"
]
RouteMode = Literal[
    "CASE_CHAT", "CONTROL", "DIAGNOSIS_FOLLOW_UP", "KNOWLEDGE", "KNOWLEDGE_IN_CASE", "HYBRID", "ATTACHMENT"
]
SlotState = Literal[
    "UNASKED", "ASKED", "ANSWERED", "UNKNOWN_BY_USER", "UNAVAILABLE", "DECLINED", "NOT_APPLICABLE"
]

# A semantic proposal may quote or classify user text that mentions SSH/tcpdump/
# reboot/etc. That is normal VOIP support language and is not execution. What is
# forbidden is a model-authored executable instruction encoded as a structured
# entity. Formal actions have their own Policy/Registry/Orchestrator contracts.
_FORBIDDEN_EXECUTION_ENTITY_KEYS = {
    "command", "commands", "raw_command", "shell_command", "ssh_command",
    "exec", "execute", "execution", "device_action", "action_id",
    "orchestrator_action", "reproduction_action", "fix_action",
}


def _contains_execution_key(value: Any) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).strip().lower() in _FORBIDDEN_EXECUTION_ENTITY_KEYS:
                return True
            if _contains_execution_key(child):
                return True
    elif isinstance(value, list):
        return any(_contains_execution_key(child) for child in value)
    return False


class ActiveQuestionAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slot_key: str = Field(min_length=1, max_length=128)
    state: SlotState
    value: Any | None = None
    confidence: float = Field(ge=0.0, le=1.0)


class ConversationTurnProposal(BaseModel):
    """Non-executing semantic interpretation for one user turn."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["conversation-turn-v1"]
    intent: TurnIntent
    classification: TurnClassification
    route_mode: RouteMode
    active_question_answer: ActiveQuestionAnswer | None = None
    entities: dict[str, Any] = Field(default_factory=dict)
    material_diagnostic_context: bool = False
    needs_clarification: bool = False
    clarification_question: str | None = Field(default=None, max_length=500)
    confidence: float = Field(ge=0.0, le=1.0)
    safety_class: Literal["NON_EXECUTING_SEMANTIC_PROPOSAL"]

    @model_validator(mode="after")
    def enforce_semantic_only(self):
        if _contains_execution_key(self.entities):
            raise ValueError("CONVERSATION_EXECUTABLE_ENTITY_FORBIDDEN")
        if self.classification in {"CHAT_ONLY", "KNOWLEDGE", "CONTROL"} and self.material_diagnostic_context:
            raise ValueError("CONVERSATION_NON_DIAGNOSTIC_CLASS_CANNOT_BE_MATERIAL")
        if self.route_mode == "HYBRID" and self.classification != "DIAGNOSTIC_CONTEXT":
            raise ValueError("CONVERSATION_HYBRID_MUST_CARRY_DIAGNOSTIC_CONTEXT")
        return self


class ResponsePlan(BaseModel):
    """Grounded response selection contract.

    The model selects identifiers from a deterministic catalog instead of writing
    protocol facts. The renderer resolves those identifiers to trusted text.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["conversation-response-plan-v1"]
    answer_mode: Literal["STATUS", "ACK", "QUESTION", "PARTIAL_CONCLUSION", "KNOWLEDGE", "CONTROL"]
    fact_ids: list[str] = Field(default_factory=list, max_length=16)
    uncertainty_ids: list[str] = Field(default_factory=list, max_length=16)
    next_action_id: str | None = Field(default=None, max_length=128)
    question_id: str | None = Field(default=None, max_length=128)
    tone: Literal["CONCISE_ENGINEER", "EXPLANATORY_ENGINEER"] = "CONCISE_ENGINEER"
    confidence: float = Field(ge=0.0, le=1.0)
    safety_class: Literal["GROUNDED_SELECTION_ONLY"]
