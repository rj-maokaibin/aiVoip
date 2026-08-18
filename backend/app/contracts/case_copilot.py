from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.diagnosis.claim_grounding import DiagnosticClaim


_CONTROL_INTENTS = Literal[
    "STOP_REPRODUCTION",
    "EXTERNAL_ACTION_COMPLETED",
    "FIX_APPLIED",
    "START_REGISTERED_EXPERIMENT",
]

_COMMAND = re.compile(
    r"(?i)(?:\b(?:ssh|telnet|bash|sh|shell|exec|system)\b|"
    r"\b(?:reboot|sysupgrade|kill|rm\s+-rf|tcpdump)\b|"
    r"\bvoip\s+dsp\s+diag\b|\baim>\b)"
)
_CONFIRMED_ROOT_CAUSE = re.compile(
    r"(?i)(?:根因(?:已经|已|可以)?(?:确认|确定)|最终根因(?:是|为)|"
    r"root\s+cause\s+(?:is\s+)?(?:confirmed|proven|established))"
)


class CopilotNextStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["READ_ONLY_GUIDANCE", "CONTROL_INTENT_REQUIRED"]
    text: str = Field(min_length=1, max_length=1000)
    intent: _CONTROL_INTENTS | None = None
    registered_id: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def validate_control_shape(self):
        if self.kind == "CONTROL_INTENT_REQUIRED" and self.intent is None:
            raise ValueError("COPILOT_CONTROL_INTENT_REQUIRED")
        if self.kind == "READ_ONLY_GUIDANCE" and self.intent is not None:
            raise ValueError("COPILOT_READ_ONLY_STEP_CANNOT_CARRY_CONTROL_INTENT")
        return self


class CaseCopilotProposal(BaseModel):
    """AI3 read-only grounded answer contract.

    Every factual/diagnostic assertion must be represented as an L5 PROPOSED
    DiagnosticClaim and structurally grounded against current-Case Evidence by
    ClaimGroundingValidator before the answer can be returned.
    """

    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["ai-case-copilot-v1"]
    answer: str = Field(min_length=1, max_length=12000)
    claims: list[DiagnosticClaim] = Field(default_factory=list, max_length=64)
    cited_evidence_ids: list[str] = Field(default_factory=list, max_length=128)
    uncertainty: list[str] = Field(default_factory=list, max_length=32)
    next_steps: list[CopilotNextStep] = Field(default_factory=list, max_length=16)
    root_cause_confirmed_by_ai: Literal[False]
    safety_class: Literal["READ_ONLY_GROUNDED_RESPONSE"]

    @model_validator(mode="after")
    def reject_execution_or_root_cause_confirmation(self):
        rendered = self.model_dump_json()
        if _COMMAND.search(rendered):
            raise ValueError("COPILOT_EXECUTABLE_CONTENT_FORBIDDEN")
        if _CONFIRMED_ROOT_CAUSE.search(self.answer):
            raise ValueError("COPILOT_ROOT_CAUSE_CONFIRMATION_FORBIDDEN")
        return self
