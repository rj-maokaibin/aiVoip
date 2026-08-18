from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SemanticIntentName = Literal[
    "NEW_DIAGNOSIS", "CASE_FOLLOW_UP", "STATUS_QUERY", "STOP_REPRODUCTION",
    "EXTERNAL_ACTION_COMPLETED", "FIX_APPLIED", "GENERAL_QUESTION", "UNSUPPORTED",
]
CaseOperation = Literal[
    "CREATE_CASE", "ADD_EVIDENCE", "ADD_EVIDENCE_AND_COMPARE", "READ_STATUS",
    "STOP_REPRODUCTION", "COMPLETE_EXTERNAL_ACTION", "UPDATE_FIX_VERIFICATION",
    "ANSWER_QUESTION", "ASK_CLARIFICATION", "NONE",
]
RequestedOperation = Literal[
    "CONTINUE_ANALYSIS", "COMPARE_ENVIRONMENTS", "READ_STATUS", "STOP_REPRODUCTION",
    "COMPLETE_EXTERNAL_ACTION", "VERIFY_FIX", "ANSWER_ONLY", "ASK_CLARIFICATION", "NONE",
]
_DANGEROUS_TEXT = re.compile(
    r"(?i)(?:\b(?:ssh|telnet|bash|sh|shell|exec|system)\b|"
    r"\b(?:reboot|sysupgrade|kill|rm\s+-rf|tcpdump)\b|"
    r"\bvoip\s+dsp\s+diag\b|\baim>\b|(?:原始|raw)\s*(?:命令|command))"
)


class SemanticDeviceRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sn: str | None = None
    ip: str | None = None
    mac: str | None = None
    product: str | None = None


class AttachmentRole(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attachment_id: str
    role: Literal[
        "NEW_REPRODUCTION_EVIDENCE", "REFERENCE_EVIDENCE", "COMPARISON_BASELINE", "UNKNOWN"
    ]


class ComparisonRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    compare_with_previous_environment: bool = False
    baseline_label: str | None = None
    candidate_label: str | None = None


class SemanticIntentProposal(BaseModel):
    """Non-executing AI1 proposal. No raw-command field exists by design."""

    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["feishu-semantic-intent-v1"]
    intent: SemanticIntentName
    case_operation: CaseOperation = "NONE"
    case_ref: str | None = None
    symptoms: list[str] = Field(default_factory=list, max_length=32)
    device_refs: list[SemanticDeviceRef] = Field(default_factory=list, max_length=16)
    environment_changes: dict[str, Any] = Field(default_factory=dict)
    temporal_clues: dict[str, Any] = Field(default_factory=dict)
    attachment_roles: list[AttachmentRole] = Field(default_factory=list, max_length=32)
    comparison_request: ComparisonRequest = Field(default_factory=ComparisonRequest)
    requested_operation: RequestedOperation = "NONE"
    confidence: float = Field(ge=0.0, le=1.0)
    missing_fields: list[str] = Field(default_factory=list, max_length=32)
    safety_class: Literal["NON_EXECUTING_PROPOSAL"]

    @field_validator("case_ref", mode="before")
    @classmethod
    def normalize_case_ref(cls, value):
        if value is None:
            return None
        text = str(value).strip()
        return text[:128] or None

    @model_validator(mode="after")
    def reject_command_material(self):
        if _DANGEROUS_TEXT.search(self.model_dump_json(exclude={"schema_version"})):
            raise ValueError("SEMANTIC_PROPOSAL_EXECUTABLE_CONTENT_FORBIDDEN")
        return self


def validate_semantic_proposal(payload: dict[str, Any]) -> SemanticIntentProposal:
    return SemanticIntentProposal.model_validate(payload)
