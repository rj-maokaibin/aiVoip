from __future__ import annotations

from datetime import datetime
from typing import Any
from pydantic import BaseModel, ConfigDict, Field


class ReproductionCreate(BaseModel):
    profile_id: str | None = None
    symptom_class: str | None = None
    device_id: str | None = None


class ReproductionSessionOut(BaseModel):
    model_config=ConfigDict(from_attributes=True)
    id: str
    case_id: str
    device_id: str
    profile_key: str
    profile_version: str
    profile_checksum: str
    state: str
    capture_stage: str
    cleanup_required: bool
    cleanup_status: str
    capture_completeness: str
    evidence_sufficiency: str
    primary_target_call_id: str | None = None
    voice_runtime_context_json: dict | None = None
    owner_worker: str | None = None
    lease_expires_at: datetime | None = None
    heartbeat_at: datetime | None = None
    retry_parent_session_id: str | None = None
    terminal_reason: str | None = None
    created_at: datetime
    updated_at: datetime


class ReproductionAttemptOut(BaseModel):
    model_config=ConfigDict(from_attributes=True)
    id: str; session_id: str; attempt_no: int; status: str; valid: bool
    start_anchor_type: str | None = None; start_anchor_ms: int | None = None
    end_anchor_type: str | None = None; end_anchor_ms: int | None = None


class ReproductionCallOut(BaseModel):
    model_config=ConfigDict(from_attributes=True)
    id: str; session_id: str; attempt_id: str | None = None; call_no: int; status: str
    verdict: str | None = None; role: str | None = None; quick_analysis_json: dict | None = None


class ReproductionProfileOut(BaseModel):
    id: str
    name: str
    version: str
    checksum: str
    symptom_classes: list[str]
    end_policy: str
    max_calls: int
    stages: list[str]


class ReproductionBundleOut(BaseModel):
    schema_version: int
    session: dict[str, Any]
    voice_runtime_context: dict[str, Any] | None = None
    arm_validations: list[dict[str, Any]]
    capture_health: dict[str, Any]
    capture_pipeline: dict[str, Any]
    attempts: list[dict[str, Any]]
    calls: list[dict[str, Any]]
    cleanup_runs: list[dict[str, Any]]
