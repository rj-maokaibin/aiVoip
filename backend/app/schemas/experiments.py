from __future__ import annotations

from datetime import datetime
from typing import Any
from pydantic import BaseModel, ConfigDict, Field


class DiagnosticQuestionTemplateOut(BaseModel):
    id: str
    version: str
    level: str
    title: str
    priority: int
    information_gain: int
    required_evidence: dict[str, Any]
    next_questions: list[str]
    next_by_route: dict[str, list[str]]
    experiment_profiles: list[str]
    checksum: str


class DiagnosticQuestionOut(BaseModel):
    model_config=ConfigDict(from_attributes=True)
    id: str
    case_id: str
    session_id: str | None = None
    parent_question_id: str | None = None
    question_key: str
    template_version: str
    template_checksum: str | None = None
    state: str
    level: str
    priority: int
    information_gain: int
    selected_reason: str | None = None
    requirements_json: dict
    answer_json: dict | None = None
    evidence_refs_json: list | None = None
    created_at: datetime
    updated_at: datetime


class QuestionAnswerRequest(BaseModel):
    answer: dict[str, Any]
    evidence_refs: list[dict[str, Any]] = Field(default_factory=list)
    route: str | None = None


class ExperimentProfileOut(BaseModel):
    id: str
    name: str
    version: str
    checksum: str
    hypothesis_codes: list[str]
    reproduction_profile_id: str
    independent_variable: str
    target_finding: str
    confirmation_policy: str
    sequence: list[str]
    external_action_required: bool
    external_action_instructions: str
    expected_change_paths: list[str]
    must_equal_paths: list[str]
    soft_drift_paths: list[str]


class ExperimentCreateRequest(BaseModel):
    profile_id: str
    hypothesis_id: str | None = None
    question_id: str | None = None


class DiagnosticExperimentOut(BaseModel):
    model_config=ConfigDict(from_attributes=True)
    id: str
    case_id: str
    hypothesis_id: str | None = None
    question_id: str | None = None
    profile_key: str
    profile_version: str
    profile_checksum: str
    state: str
    confirmation_policy: str
    independent_variable: str
    target_finding: str
    reproduction_profile_id: str
    current_round: int
    causal_state: str
    terminal_reason: str | None = None
    created_at: datetime
    updated_at: datetime


class ExperimentRunOut(BaseModel):
    model_config=ConfigDict(from_attributes=True)
    id: str
    experiment_id: str
    case_id: str
    run_no: int
    variant: str
    status: str
    reproduction_session_id: str | None = None
    reproduction_call_id: str | None = None
    target_verdict: str | None = None
    target_finding_present: bool | None = None
    metrics_json: dict | None = None
    external_action_required: bool
    external_action_completed_at: datetime | None = None
    created_at: datetime


class ExperimentStartReproductionRequest(BaseModel):
    external_state: dict[str, Any] = Field(default_factory=dict)
    call_context: dict[str, Any] = Field(default_factory=dict)
    environment_overrides: dict[str, Any] | None = None


class ExperimentAttachResultRequest(BaseModel):
    session_id: str
    call_id: str
    external_state: dict[str, Any]
    call_context: dict[str, Any]
    environment_overrides: dict[str, Any] | None = None
    hard_contradictions: list[str] = Field(default_factory=list)


class EnvironmentComparisonOut(BaseModel):
    model_config=ConfigDict(from_attributes=True)
    id: str
    experiment_id: str
    baseline_run_id: str
    variant_run_id: str
    status: str
    expected_changes_json: list | None = None
    soft_drift_json: list | None = None
    hard_drift_json: list | None = None
    compared_fields_json: dict | None = None
    created_at: datetime


class CausalAssessmentOut(BaseModel):
    model_config=ConfigDict(from_attributes=True)
    id: str
    experiment_id: str
    case_id: str
    hypothesis_id: str | None = None
    state: str
    confirmation_policy: str
    supporting_run_ids_json: list
    environment_comparison_ids_json: list
    hard_contradictions_json: list | None = None
    rationale_json: dict
    created_at: datetime


class FixActionCreateRequest(BaseModel):
    action_type: str
    description: str
    hypothesis_id: str | None = None
    experiment_id: str | None = None
    version_before: str | None = None
    version_after: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class FixActionOut(BaseModel):
    model_config=ConfigDict(from_attributes=True)
    id: str
    case_id: str
    hypothesis_id: str | None = None
    experiment_id: str | None = None
    action_type: str
    description: str
    version_before: str | None = None
    version_after: str | None = None
    actor: str | None = None
    metadata_json: dict | None = None
    created_at: datetime


class FixVerificationCreateRequest(BaseModel):
    baseline_session_id: str
    baseline_call_id: str
    target_finding: str
    reproduction_profile_id: str | None = None
    required_calls: int = 1
    max_calls: int = 3


class FixVerificationEvaluateRequest(BaseModel):
    verification_session_id: str
    verification_call_id: str
    baseline_environment: dict[str, Any]
    verification_environment: dict[str, Any]
    business_checks: dict[str, bool]
    new_blocking_findings: list[str] = Field(default_factory=list)


class FixVerificationOut(BaseModel):
    model_config=ConfigDict(from_attributes=True)
    id: str
    case_id: str
    fix_action_id: str
    baseline_session_id: str | None = None
    verification_session_id: str | None = None
    baseline_call_id: str | None = None
    verification_call_id: str | None = None
    reproduction_profile_id: str
    target_finding: str
    required_calls: int
    max_calls: int
    verification_call_count: int
    successful_call_count: int
    evaluations_json: list | None = None
    status: str
    environment_status: str | None = None
    business_checks_json: dict | None = None
    comparison_json: dict | None = None
    evidence_id: str | None = None
    created_at: datetime
    updated_at: datetime
