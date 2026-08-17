from __future__ import annotations

from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field


class EvidenceReportRebuildRequest(BaseModel):
    force: bool = True


class EvidenceBundleRequest(BaseModel):
    profile: str = Field(default="INTERNAL_FULL", pattern="^(INTERNAL_FULL|SHARE_SAFE)$")


class EvidenceReportOut(BaseModel):
    model_config=ConfigDict(from_attributes=True)
    id: str
    case_id: str
    session_id: str | None = None
    call_id: str | None = None
    scope_type: str
    scope_id: str
    version: int
    status: str
    schema_version: str
    composer_version: str
    input_snapshot_hash: str
    environment_fingerprint: str | None = None
    completeness_json: dict | None = None
    boundary_json: dict | None = None
    snapshot_json: dict | None = None
    json_object_key: str | None = None
    html_object_key: str | None = None
    manifest_object_key: str | None = None
    bundle_object_key: str | None = None
    supersedes_report_id: str | None = None
    created_by: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime
    completed_at: datetime | None = None


class EvidenceFindingOut(BaseModel):
    model_config=ConfigDict(from_attributes=True)
    id: str
    case_id: str
    session_id: str | None = None
    call_id: str | None = None
    scope_type: str
    scope_id: str
    stable_key: str
    finding_signature: str
    signature_version: str
    finding_type: str
    status: str
    severity: str
    evidence_level: str
    title: str
    observation: str
    interpretation: str | None = None
    root_cause_boundary: str
    start_time: float | None = None
    end_time: float | None = None
    representative_time: float | None = None
    scope_json: dict | None = None
    metrics_json: dict | None = None
    evidence_refs_json: list = Field(default_factory=list)
    artifact_refs_json: list = Field(default_factory=list)
    event_refs_json: list = Field(default_factory=list)
    correlation_json: dict | None = None
    source_analyzer_run_ids: list = Field(default_factory=list)
    occurrence_count: int
    first_seen_report_version: int
    last_seen_report_version: int
    created_at: datetime
    updated_at: datetime
