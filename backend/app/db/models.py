from datetime import datetime, timezone
from sqlalchemy import String, Text, Integer, BigInteger, DateTime, ForeignKey, JSON, UniqueConstraint, Boolean
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db.base import Base
from app.core.ids import new_id
from app.contracts.enums import (
    CaseStatus, JobStatus, RunStatus, EvidenceKind, EvidenceScope, EvidenceLevel,
    EvidenceCompleteness, HypothesisState, IdempotencyStatus, DependencyPolicy,
    DiagnosisRunStatus, CollectionPlanStatus, RuleVersionStatus, KnowledgeStatus, ReportStatus,
    ReproductionState, ReproductionProfileStatus, CaptureStage, AttemptStatus, CallVerdict, CallRole,
    ChannelHealth, ArmValidationStatus, CleanupStatus, EvidenceSufficiency, CleanupRunStatus, LockStatus,
    ReproductionCallStatus, DiagnosticQuestionState, EndPolicy,
    ExperimentState, ExperimentRunStatus, EnvironmentComparisonStatus, CausalConclusionState,
    ConfirmationPolicy, FixActionType, FixVerificationStatus, FixVerificationRunStatus,
)

def utcnow(): return datetime.now(timezone.utc)

class Case(Base):
    __tablename__='cases'
    id: Mapped[str]=mapped_column(String(36), primary_key=True, default=new_id)
    case_no: Mapped[str]=mapped_column(String(64), unique=True, index=True)
    summary: Mapped[str]=mapped_column(Text)
    status: Mapped[str]=mapped_column(String(32), index=True, default=CaseStatus.NEW.value)
    created_by: Mapped[str|None]=mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    devices=relationship('CaseDevice', cascade='all,delete-orphan')

class CaseDevice(Base):
    __tablename__='case_devices'
    id: Mapped[str]=mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str]=mapped_column(ForeignKey('cases.id', ondelete='CASCADE'), index=True)
    ip: Mapped[str]=mapped_column(String(128)); ssh_port: Mapped[int]=mapped_column(Integer, default=22)
    sn: Mapped[str]=mapped_column(String(128)); username: Mapped[str]=mapped_column(String(64), default='admin')
    platform_id: Mapped[str|None]=mapped_column(String(128), nullable=True)
    device_info: Mapped[dict|None]=mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow)

class CaseStateHistory(Base):
    __tablename__='case_state_history'
    id: Mapped[str]=mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str]=mapped_column(ForeignKey('cases.id', ondelete='CASCADE'), index=True)
    from_status: Mapped[str|None]=mapped_column(String(32), nullable=True)
    to_status: Mapped[str]=mapped_column(String(32), index=True)
    event: Mapped[str|None]=mapped_column(String(64), nullable=True, index=True)
    actor: Mapped[str|None]=mapped_column(String(128), nullable=True)
    reason: Mapped[str|None]=mapped_column(Text, nullable=True)
    context_json: Mapped[dict|None]=mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow)

class Job(Base):
    __tablename__='jobs'
    id: Mapped[str]=mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str]=mapped_column(ForeignKey('cases.id', ondelete='CASCADE'), index=True)
    type: Mapped[str]=mapped_column(String(64)); status: Mapped[str]=mapped_column(String(32), index=True, default=JobStatus.PENDING.value)
    profile_id: Mapped[str|None]=mapped_column(String(128), nullable=True)
    error_code: Mapped[str|None]=mapped_column(String(128), nullable=True); error_message: Mapped[str|None]=mapped_column(Text, nullable=True)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True); finished_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)

class JobStateHistory(Base):
    __tablename__='job_state_history'
    id: Mapped[str]=mapped_column(String(36), primary_key=True, default=new_id)
    job_id: Mapped[str]=mapped_column(ForeignKey('jobs.id', ondelete='CASCADE'), index=True)
    case_id: Mapped[str]=mapped_column(ForeignKey('cases.id', ondelete='CASCADE'), index=True)
    from_status: Mapped[str|None]=mapped_column(String(32), nullable=True)
    to_status: Mapped[str]=mapped_column(String(32), index=True)
    actor: Mapped[str|None]=mapped_column(String(128), nullable=True)
    reason: Mapped[str|None]=mapped_column(Text, nullable=True)
    detail_json: Mapped[dict|None]=mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow)

class ActionRun(Base):
    __tablename__='action_runs'
    id: Mapped[str]=mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str]=mapped_column(ForeignKey('cases.id', ondelete='CASCADE'), index=True)
    job_id: Mapped[str]=mapped_column(ForeignKey('jobs.id', ondelete='CASCADE'), index=True)
    device_id: Mapped[str]=mapped_column(ForeignKey('case_devices.id', ondelete='CASCADE'))
    action_id: Mapped[str]=mapped_column(String(128)); risk_level: Mapped[str]=mapped_column(String(8)); status: Mapped[str]=mapped_column(String(32))
    exit_status: Mapped[int|None]=mapped_column(Integer, nullable=True); error_message: Mapped[str|None]=mapped_column(Text, nullable=True)
    started_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow); finished_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)

class Evidence(Base):
    __tablename__='evidences'
    id: Mapped[str]=mapped_column(String(36), primary_key=True, default=new_id)
    # Raw evidence must outlive ordinary Case lifecycle operations.
    case_id: Mapped[str]=mapped_column(ForeignKey('cases.id', ondelete='RESTRICT'), index=True)
    device_id: Mapped[str|None]=mapped_column(ForeignKey('case_devices.id', ondelete='SET NULL'), nullable=True)
    job_id: Mapped[str|None]=mapped_column(ForeignKey('jobs.id', ondelete='SET NULL'), nullable=True)
    action_run_id: Mapped[str|None]=mapped_column(ForeignKey('action_runs.id', ondelete='SET NULL'), nullable=True)
    type: Mapped[str]=mapped_column(String(64), index=True)
    source: Mapped[str]=mapped_column(String(64))
    kind: Mapped[str]=mapped_column(String(16), default=EvidenceKind.RAW.value, index=True)
    source_scope: Mapped[str]=mapped_column(String(32), default=EvidenceScope.CASE.value, index=True)
    level: Mapped[str]=mapped_column(String(8), default=EvidenceLevel.L1.value, index=True)
    completeness: Mapped[str]=mapped_column(String(32), default=EvidenceCompleteness.COMPLETE.value, index=True)
    filename: Mapped[str]=mapped_column(String(512))
    object_key: Mapped[str]=mapped_column(String(1024))
    size_bytes: Mapped[int]=mapped_column(BigInteger)
    sha256: Mapped[str]=mapped_column(String(64), index=True)
    content_type: Mapped[str|None]=mapped_column(String(128), nullable=True)
    captured_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)
    time_range_start: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)
    time_range_end: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)
    producer_type: Mapped[str|None]=mapped_column(String(64), nullable=True)
    producer_id: Mapped[str|None]=mapped_column(String(128), nullable=True, index=True)
    producer_version: Mapped[str|None]=mapped_column(String(64), nullable=True)
    session_id: Mapped[str|None]=mapped_column(String(36), nullable=True, index=True)
    attempt_id: Mapped[str|None]=mapped_column(String(36), nullable=True, index=True)
    call_id: Mapped[str|None]=mapped_column(String(36), nullable=True, index=True)
    metadata_json: Mapped[dict|None]=mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow)

class EvidenceRelation(Base):
    __tablename__='evidence_relations'
    __table_args__=(UniqueConstraint('parent_evidence_id','child_evidence_id','relation_type',name='uq_evidence_relation'),)
    id: Mapped[str]=mapped_column(String(36), primary_key=True, default=new_id)
    parent_evidence_id: Mapped[str]=mapped_column(ForeignKey('evidences.id', ondelete='RESTRICT'), index=True)
    child_evidence_id: Mapped[str]=mapped_column(ForeignKey('evidences.id', ondelete='RESTRICT'), index=True)
    relation_type: Mapped[str]=mapped_column(String(32), index=True, default='DERIVED_FROM')
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow)


class Artifact(Base):
    __tablename__='artifacts'
    id: Mapped[str]=mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str]=mapped_column(ForeignKey('cases.id', ondelete='CASCADE'), index=True)
    analyzer_run_id: Mapped[str|None]=mapped_column(ForeignKey('analyzer_runs.id', ondelete='SET NULL'), nullable=True, index=True)
    evidence_id: Mapped[str|None]=mapped_column(ForeignKey('evidences.id', ondelete='SET NULL'), nullable=True, index=True)
    type: Mapped[str]=mapped_column(String(64), index=True)
    filename: Mapped[str]=mapped_column(String(512))
    object_key: Mapped[str]=mapped_column(String(1024), unique=True)
    content_type: Mapped[str|None]=mapped_column(String(128), nullable=True)
    size_bytes: Mapped[int]=mapped_column(BigInteger)
    sha256: Mapped[str]=mapped_column(String(64), index=True)
    metadata_json: Mapped[dict|None]=mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow)

class AnalyzerRun(Base):
    __tablename__='analyzer_runs'
    id: Mapped[str]=mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str]=mapped_column(ForeignKey('cases.id', ondelete='CASCADE'), index=True)
    job_id: Mapped[str|None]=mapped_column(ForeignKey('jobs.id', ondelete='SET NULL'), nullable=True, index=True)
    analyzer_name: Mapped[str]=mapped_column(String(128), index=True)
    analyzer_version: Mapped[str]=mapped_column(String(64))
    config_version: Mapped[str|None]=mapped_column(String(128), nullable=True)
    config_checksum: Mapped[str|None]=mapped_column(String(64), nullable=True, index=True)
    config_snapshot: Mapped[dict|None]=mapped_column(JSON, nullable=True)
    scope: Mapped[str]=mapped_column(String(32), default=EvidenceScope.CASE.value, index=True)
    status: Mapped[str]=mapped_column(String(32), index=True, default=DiagnosisRunStatus.PENDING.value)
    input_evidence_ids: Mapped[list]=mapped_column(JSON)
    output_evidence_ids: Mapped[list|None]=mapped_column(JSON, nullable=True)
    summary_json: Mapped[dict|None]=mapped_column(JSON, nullable=True)
    result_object_key: Mapped[str|None]=mapped_column(String(1024), nullable=True)
    error_code: Mapped[str|None]=mapped_column(String(128), nullable=True)
    error_message: Mapped[str|None]=mapped_column(Text, nullable=True)
    started_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow)


class DiagnosisRun(Base):
    __tablename__='diagnosis_runs'
    id: Mapped[str]=mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str]=mapped_column(ForeignKey('cases.id', ondelete='CASCADE'), index=True)
    job_id: Mapped[str|None]=mapped_column(ForeignKey('jobs.id', ondelete='SET NULL'), nullable=True, index=True)
    status: Mapped[str]=mapped_column(String(32), index=True, default=RunStatus.PENDING.value)
    cycle: Mapped[int]=mapped_column(Integer, default=0)
    reasoner_name: Mapped[str]=mapped_column(String(128), default='deterministic')
    reasoner_version: Mapped[str]=mapped_column(String(64), default='0.1.0')
    workflow_version: Mapped[str]=mapped_column(String(64), default='m4-v1')
    prompt_version: Mapped[str|None]=mapped_column(String(64), nullable=True)
    model_name: Mapped[str|None]=mapped_column(String(128), nullable=True)
    last_fingerprint: Mapped[str|None]=mapped_column(String(64), nullable=True)
    no_progress_count: Mapped[int]=mapped_column(Integer, default=0)
    summary_json: Mapped[dict|None]=mapped_column(JSON, nullable=True)
    decision_json: Mapped[dict|None]=mapped_column(JSON, nullable=True)
    started_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

class Hypothesis(Base):
    __tablename__='hypotheses'
    __table_args__=(UniqueConstraint('case_id','code',name='uq_hypotheses_case_code'),)
    id: Mapped[str]=mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str]=mapped_column(ForeignKey('cases.id', ondelete='CASCADE'), index=True)
    diagnosis_run_id: Mapped[str|None]=mapped_column(ForeignKey('diagnosis_runs.id', ondelete='SET NULL'), nullable=True, index=True)
    code: Mapped[str]=mapped_column(String(128), index=True)
    title: Mapped[str]=mapped_column(Text)
    fault_domain: Mapped[str]=mapped_column(String(128), index=True)
    status: Mapped[str]=mapped_column(String(32), index=True, default=HypothesisState.OPEN.value)
    confidence: Mapped[int]=mapped_column(Integer, default=0)  # 0..10000，避免浮点跨DB差异
    rationale: Mapped[str|None]=mapped_column(Text, nullable=True)
    confirmable: Mapped[int]=mapped_column(Integer, default=0)
    confirm_rule: Mapped[str|None]=mapped_column(String(128), nullable=True)
    current_revision_id: Mapped[str|None]=mapped_column(String(36), nullable=True, index=True)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

class HypothesisEvidence(Base):
    __tablename__='hypothesis_evidence'
    id: Mapped[str]=mapped_column(String(36), primary_key=True, default=new_id)
    hypothesis_id: Mapped[str]=mapped_column(ForeignKey('hypotheses.id', ondelete='CASCADE'), index=True)
    hypothesis_revision_id: Mapped[str|None]=mapped_column(ForeignKey('hypothesis_revisions.id', ondelete='CASCADE'), nullable=True, index=True)
    ref_type: Mapped[str]=mapped_column(String(64))
    ref_id: Mapped[str]=mapped_column(String(128), index=True)
    evidence_level: Mapped[str]=mapped_column(String(8), index=True)
    direction: Mapped[str]=mapped_column(String(16), default='SUPPORT')
    weight: Mapped[int]=mapped_column(Integer, default=1000)
    rationale: Mapped[str|None]=mapped_column(Text, nullable=True)
    details_json: Mapped[dict|None]=mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow)

class HypothesisRevision(Base):
    __tablename__='hypothesis_revisions'
    __table_args__=(UniqueConstraint('hypothesis_id','revision_no',name='uq_hypothesis_revision_no'),)
    id: Mapped[str]=mapped_column(String(36), primary_key=True, default=new_id)
    hypothesis_id: Mapped[str]=mapped_column(ForeignKey('hypotheses.id', ondelete='CASCADE'), index=True)
    diagnosis_run_id: Mapped[str|None]=mapped_column(ForeignKey('diagnosis_runs.id', ondelete='SET NULL'), nullable=True, index=True)
    revision_no: Mapped[int]=mapped_column(Integer)
    supersedes_revision_id: Mapped[str|None]=mapped_column(ForeignKey('hypothesis_revisions.id', ondelete='RESTRICT'), nullable=True, index=True)
    title: Mapped[str]=mapped_column(Text)
    fault_domain: Mapped[str]=mapped_column(String(128), index=True)
    status: Mapped[str]=mapped_column(String(32), index=True)
    confidence: Mapped[int]=mapped_column(Integer, default=0)
    rationale: Mapped[str|None]=mapped_column(Text, nullable=True)
    confirmable: Mapped[int]=mapped_column(Integer, default=0)
    confirm_rule: Mapped[str|None]=mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow)

class JobDependency(Base):
    __tablename__='job_dependencies'
    __table_args__=(UniqueConstraint('job_id','depends_on_job_id',name='uq_job_dependency'),)
    id: Mapped[str]=mapped_column(String(36), primary_key=True, default=new_id)
    job_id: Mapped[str]=mapped_column(ForeignKey('jobs.id', ondelete='CASCADE'), index=True)
    depends_on_job_id: Mapped[str]=mapped_column(ForeignKey('jobs.id', ondelete='RESTRICT'), index=True)
    policy: Mapped[str]=mapped_column(String(64), default=DependencyPolicy.WAIT_ALL_SUCCESS.value)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow)

class CollectionPlan(Base):
    __tablename__='collection_plans'
    id: Mapped[str]=mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str]=mapped_column(ForeignKey('cases.id', ondelete='CASCADE'), index=True)
    diagnosis_run_id: Mapped[str]=mapped_column(ForeignKey('diagnosis_runs.id', ondelete='CASCADE'), index=True)
    cycle: Mapped[int]=mapped_column(Integer)
    status: Mapped[str]=mapped_column(String(32), index=True, default=CollectionPlanStatus.PROPOSED.value)
    goal: Mapped[str]=mapped_column(Text)
    actions_json: Mapped[list]=mapped_column(JSON)
    execution_job_ids: Mapped[list|None]=mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

class AuditLog(Base):
    __tablename__='audit_logs'
    id: Mapped[str]=mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str|None]=mapped_column(String(36), nullable=True, index=True)
    actor: Mapped[str|None]=mapped_column(String(128), nullable=True, index=True)
    actor_type: Mapped[str|None]=mapped_column(String(32), nullable=True, index=True)
    event_type: Mapped[str]=mapped_column(String(128), index=True)
    action: Mapped[str|None]=mapped_column(String(128), nullable=True, index=True)
    target_type: Mapped[str|None]=mapped_column(String(64), nullable=True, index=True)
    target_id: Mapped[str|None]=mapped_column(String(128), nullable=True, index=True)
    before_json: Mapped[dict|None]=mapped_column(JSON, nullable=True)
    after_json: Mapped[dict|None]=mapped_column(JSON, nullable=True)
    reason: Mapped[str|None]=mapped_column(Text, nullable=True)
    trace_id: Mapped[str|None]=mapped_column(String(128), nullable=True, index=True)
    detail: Mapped[dict|None]=mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow, index=True)

class IdempotencyRecord(Base):
    __tablename__='idempotency_records'
    __table_args__=(UniqueConstraint('scope','idempotency_key',name='uq_idempotency_scope_key'),)
    id: Mapped[str]=mapped_column(String(36), primary_key=True, default=new_id)
    scope: Mapped[str]=mapped_column(String(128), index=True)
    idempotency_key: Mapped[str]=mapped_column(String(255))
    request_hash: Mapped[str]=mapped_column(String(64))
    status: Mapped[str]=mapped_column(String(32), default=IdempotencyStatus.IN_PROGRESS.value, index=True)
    response_status: Mapped[int|None]=mapped_column(Integer, nullable=True)
    response_json: Mapped[dict|None]=mapped_column(JSON, nullable=True)
    resource_type: Mapped[str|None]=mapped_column(String(64), nullable=True)
    resource_id: Mapped[str|None]=mapped_column(String(128), nullable=True, index=True)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    expires_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True, index=True)

class EventOutbox(Base):
    __tablename__='event_outbox'
    seq: Mapped[int]=mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str]=mapped_column(String(36), unique=True, index=True, default=new_id)
    event_type: Mapped[str]=mapped_column(String(128), index=True)
    schema_version: Mapped[int]=mapped_column(Integer, default=1)
    case_id: Mapped[str|None]=mapped_column(String(36), nullable=True, index=True)
    entity_type: Mapped[str|None]=mapped_column(String(64), nullable=True)
    entity_id: Mapped[str|None]=mapped_column(String(128), nullable=True, index=True)
    payload_json: Mapped[dict|None]=mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow, index=True)

class FeishuCaseBinding(Base):
    __tablename__='feishu_case_bindings'
    __table_args__=(UniqueConstraint('case_id',name='uq_feishu_case_binding_case'),)
    id: Mapped[str]=mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str]=mapped_column(ForeignKey('cases.id', ondelete='CASCADE'), index=True)
    receive_id: Mapped[str]=mapped_column(String(256))
    receive_id_type: Mapped[str]=mapped_column(String(32), default='chat_id')
    # Nullable so a Case can be bound to its source chat BEFORE the first card is
    # sent (provision time); message_id is backfilled on first sync_case_card.
    message_id: Mapped[str|None]=mapped_column(String(256), unique=True, index=True, nullable=True)
    status: Mapped[str]=mapped_column(String(32), default='ACTIVE', index=True)
    card_version: Mapped[int]=mapped_column(Integer, default=1)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class DeviceCredential(Base):
    """DUT SSH credential resolved from Poseidon and provisioned for background
    reproduction. Stored in the DB (not in secret.yaml) so the reproduction platform
    reads host/port/user/password from here. Password is never returned to Feishu and
    never logged.
    """
    __tablename__='device_credentials'
    __table_args__=(UniqueConstraint('sn', name='uq_device_credential_sn'),)
    id: Mapped[str]=mapped_column(String(36), primary_key=True, default=new_id)
    sn: Mapped[str]=mapped_column(String(128), unique=True, index=True)
    ip: Mapped[str]=mapped_column(String(128))
    ssh_port: Mapped[int]=mapped_column(Integer, default=22)
    username: Mapped[str]=mapped_column(String(64), default='root')
    password: Mapped[str]=mapped_column(Text)
    mac: Mapped[str|None]=mapped_column(String(64), nullable=True)
    product: Mapped[str|None]=mapped_column(String(128), nullable=True)
    web_url: Mapped[str|None]=mapped_column(Text, nullable=True)
    source: Mapped[str]=mapped_column(String(32), default='poseidon')
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class User(Base):
    __tablename__='users'
    id: Mapped[str]=mapped_column(String(36), primary_key=True, default=new_id)
    external_subject: Mapped[str]=mapped_column(String(256), unique=True, index=True)
    display_name: Mapped[str|None]=mapped_column(String(256), nullable=True)
    active: Mapped[bool]=mapped_column(Boolean, default=True)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow)

class Role(Base):
    __tablename__='roles'
    id: Mapped[str]=mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str]=mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow)

class Permission(Base):
    __tablename__='permissions'
    id: Mapped[str]=mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str]=mapped_column(String(128), unique=True, index=True)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow)

class UserRole(Base):
    __tablename__='user_roles'
    __table_args__=(UniqueConstraint('user_id','role_id',name='uq_user_role'),)
    id: Mapped[str]=mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str]=mapped_column(ForeignKey('users.id', ondelete='CASCADE'), index=True)
    role_id: Mapped[str]=mapped_column(ForeignKey('roles.id', ondelete='RESTRICT'), index=True)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow)

class RolePermission(Base):
    __tablename__='role_permissions'
    __table_args__=(UniqueConstraint('role_id','permission_id',name='uq_role_permission'),)
    id: Mapped[str]=mapped_column(String(36), primary_key=True, default=new_id)
    role_id: Mapped[str]=mapped_column(ForeignKey('roles.id', ondelete='CASCADE'), index=True)
    permission_id: Mapped[str]=mapped_column(ForeignKey('permissions.id', ondelete='RESTRICT'), index=True)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow)

class RuleDefinition(Base):
    __tablename__='rule_definitions'
    id: Mapped[str]=mapped_column(String(36), primary_key=True, default=new_id)
    rule_key: Mapped[str]=mapped_column(String(128), unique=True, index=True)
    name: Mapped[str]=mapped_column(String(256))
    fault_domain: Mapped[str]=mapped_column(String(128), index=True, default='Other')
    enabled: Mapped[int]=mapped_column(Integer, default=1)
    active_version: Mapped[str|None]=mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

class RuleVersion(Base):
    __tablename__='rule_versions'
    __table_args__=(UniqueConstraint('rule_definition_id','version',name='uq_rule_version'),)
    id: Mapped[str]=mapped_column(String(36), primary_key=True, default=new_id)
    rule_definition_id: Mapped[str]=mapped_column(ForeignKey('rule_definitions.id', ondelete='CASCADE'), index=True)
    version: Mapped[str]=mapped_column(String(64))
    checksum: Mapped[str]=mapped_column(String(64), index=True)
    status: Mapped[str]=mapped_column(String(32), index=True, default=RuleVersionStatus.DRAFT.value)
    content_json: Mapped[dict]=mapped_column(JSON)
    created_by: Mapped[str|None]=mapped_column(String(128), nullable=True)
    approved_by: Mapped[str|None]=mapped_column(String(128), nullable=True)
    change_note: Mapped[str|None]=mapped_column(Text, nullable=True)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow)
    approved_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)

class RuleReplayRun(Base):
    __tablename__='rule_replay_runs'
    id: Mapped[str]=mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str]=mapped_column(ForeignKey('cases.id', ondelete='CASCADE'), index=True)
    rule_version_id: Mapped[str]=mapped_column(ForeignKey('rule_versions.id', ondelete='CASCADE'), index=True)
    status: Mapped[str]=mapped_column(String(32), index=True, default=RunStatus.PENDING.value)
    input_fingerprint: Mapped[str|None]=mapped_column(String(64), nullable=True)
    matched: Mapped[int]=mapped_column(Integer, default=0)
    result_json: Mapped[dict|None]=mapped_column(JSON, nullable=True)
    created_by: Mapped[str|None]=mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)

class KnowledgeItem(Base):
    __tablename__='knowledge_items'
    id: Mapped[str]=mapped_column(String(36), primary_key=True, default=new_id)
    type: Mapped[str]=mapped_column(String(64), index=True)
    title: Mapped[str]=mapped_column(String(512))
    summary: Mapped[str]=mapped_column(Text)
    content_json: Mapped[dict|None]=mapped_column(JSON, nullable=True)
    tags_json: Mapped[list|None]=mapped_column(JSON, nullable=True)
    source_ref: Mapped[str|None]=mapped_column(String(1024), nullable=True)
    status: Mapped[str]=mapped_column(String(32), index=True, default=KnowledgeStatus.ACTIVE.value)
    verified: Mapped[int]=mapped_column(Integer, default=0)
    verified_by: Mapped[str|None]=mapped_column(String(128), nullable=True)
    verified_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[str|None]=mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

class CaseRelation(Base):
    __tablename__='case_relations'
    __table_args__=(UniqueConstraint('case_id','related_case_id','relation_type',name='uq_case_relation'),)
    id: Mapped[str]=mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str]=mapped_column(ForeignKey('cases.id', ondelete='CASCADE'), index=True)
    related_case_id: Mapped[str]=mapped_column(ForeignKey('cases.id', ondelete='CASCADE'), index=True)
    relation_type: Mapped[str]=mapped_column(String(64), index=True, default='SIMILAR')
    score: Mapped[int]=mapped_column(Integer, default=0)
    details_json: Mapped[dict|None]=mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

class DiagnosisReport(Base):
    __tablename__='diagnosis_reports'
    id: Mapped[str]=mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str]=mapped_column(ForeignKey('cases.id', ondelete='CASCADE'), index=True)
    diagnosis_run_id: Mapped[str|None]=mapped_column(ForeignKey('diagnosis_runs.id', ondelete='SET NULL'), nullable=True, index=True)
    version: Mapped[str]=mapped_column(String(64), default='1.0')
    status: Mapped[str]=mapped_column(String(32), index=True, default=ReportStatus.GENERATED.value)
    html_object_key: Mapped[str]=mapped_column(String(1024))
    json_object_key: Mapped[str]=mapped_column(String(1024))
    snapshot_json: Mapped[dict|None]=mapped_column(JSON, nullable=True)
    created_by: Mapped[str|None]=mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow)


# ---------------------------------------------------------------------------
# M6.2 Reproduction Intelligence (Phase C mock-platform core)
# EC-02 real DUT commands remain RESERVED. These models contain no commands.
# ---------------------------------------------------------------------------

class ReproductionProfile(Base):
    __tablename__='reproduction_profiles'
    id: Mapped[str]=mapped_column(String(36), primary_key=True, default=new_id)
    profile_key: Mapped[str]=mapped_column(String(128), unique=True, index=True)
    name: Mapped[str]=mapped_column(String(256))
    active_version: Mapped[str|None]=mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ReproductionProfileVersion(Base):
    __tablename__='reproduction_profile_versions'
    __table_args__=(UniqueConstraint('profile_id','version',name='uq_reproduction_profile_version'),)
    id: Mapped[str]=mapped_column(String(36), primary_key=True, default=new_id)
    profile_id: Mapped[str]=mapped_column(ForeignKey('reproduction_profiles.id', ondelete='CASCADE'), index=True)
    version: Mapped[str]=mapped_column(String(64))
    checksum: Mapped[str]=mapped_column(String(64), index=True)
    status: Mapped[str]=mapped_column(String(32), index=True, default=ReproductionProfileStatus.DRAFT.value)
    content_json: Mapped[dict]=mapped_column(JSON)
    created_by: Mapped[str|None]=mapped_column(String(128), nullable=True)
    approved_by: Mapped[str|None]=mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow)
    approved_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)


class ReproductionSession(Base):
    __tablename__='reproduction_sessions'
    id: Mapped[str]=mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str]=mapped_column(ForeignKey('cases.id', ondelete='CASCADE'), index=True)
    device_id: Mapped[str]=mapped_column(ForeignKey('case_devices.id', ondelete='RESTRICT'), index=True)
    profile_key: Mapped[str]=mapped_column(String(128), index=True)
    profile_version: Mapped[str]=mapped_column(String(64))
    profile_checksum: Mapped[str]=mapped_column(String(64), index=True)
    effective_profile_snapshot: Mapped[dict]=mapped_column(JSON)
    platform_profile_id: Mapped[str|None]=mapped_column(String(128), nullable=True)
    platform_profile_version: Mapped[str|None]=mapped_column(String(64), nullable=True)
    state: Mapped[str]=mapped_column(String(40), index=True, default=ReproductionState.CREATED.value)
    capture_stage: Mapped[str]=mapped_column(String(32), index=True, default=CaptureStage.BASE.value)
    cleanup_required: Mapped[bool]=mapped_column(Boolean, default=False)
    cleanup_status: Mapped[str]=mapped_column(String(40), index=True, default=CleanupStatus.NOT_REQUIRED.value)
    capture_completeness: Mapped[str]=mapped_column(String(32), index=True, default=EvidenceCompleteness.UNAVAILABLE.value)
    evidence_sufficiency: Mapped[str]=mapped_column(String(64), index=True, default=EvidenceSufficiency.NOT_EVALUATED.value)
    primary_target_call_id: Mapped[str|None]=mapped_column(String(36), nullable=True, index=True)
    voice_runtime_context_json: Mapped[dict|None]=mapped_column(JSON, nullable=True)
    owner_worker: Mapped[str|None]=mapped_column(String(128), nullable=True, index=True)
    lease_expires_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True, index=True)
    heartbeat_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)
    retry_parent_session_id: Mapped[str|None]=mapped_column(ForeignKey('reproduction_sessions.id', ondelete='SET NULL'), nullable=True, index=True)
    terminal_reason: Mapped[str|None]=mapped_column(String(128), nullable=True)
    terminal_detail_json: Mapped[dict|None]=mapped_column(JSON, nullable=True)
    started_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ReproductionAttempt(Base):
    __tablename__='reproduction_attempts'
    __table_args__=(UniqueConstraint('session_id','attempt_no',name='uq_reproduction_attempt_no'),)
    id: Mapped[str]=mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str]=mapped_column(ForeignKey('reproduction_sessions.id', ondelete='CASCADE'), index=True)
    case_id: Mapped[str]=mapped_column(ForeignKey('cases.id', ondelete='CASCADE'), index=True)
    attempt_no: Mapped[int]=mapped_column(Integer)
    status: Mapped[str]=mapped_column(String(32), index=True, default=AttemptStatus.ACTIVE.value)
    valid: Mapped[bool]=mapped_column(Boolean, default=False)
    start_anchor_type: Mapped[str|None]=mapped_column(String(64), nullable=True)
    start_anchor_ms: Mapped[int|None]=mapped_column(BigInteger, nullable=True)
    end_anchor_type: Mapped[str|None]=mapped_column(String(64), nullable=True)
    end_anchor_ms: Mapped[int|None]=mapped_column(BigInteger, nullable=True)
    reconstructed_start_ms: Mapped[int|None]=mapped_column(BigInteger, nullable=True)
    details_json: Mapped[dict|None]=mapped_column(JSON, nullable=True)
    started_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow)
    ended_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)


class ReproductionCall(Base):
    __tablename__='reproduction_calls'
    __table_args__=(UniqueConstraint('session_id','call_no',name='uq_reproduction_call_no'),)
    id: Mapped[str]=mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str]=mapped_column(ForeignKey('reproduction_sessions.id', ondelete='CASCADE'), index=True)
    attempt_id: Mapped[str|None]=mapped_column(ForeignKey('reproduction_attempts.id', ondelete='SET NULL'), nullable=True, index=True)
    case_id: Mapped[str]=mapped_column(ForeignKey('cases.id', ondelete='CASCADE'), index=True)
    call_no: Mapped[int]=mapped_column(Integer)
    external_call_ref: Mapped[str|None]=mapped_column(String(128), nullable=True, index=True)
    status: Mapped[str]=mapped_column(String(32), index=True, default=ReproductionCallStatus.ACTIVE.value)
    verdict: Mapped[str|None]=mapped_column(String(32), nullable=True, index=True)
    role: Mapped[str|None]=mapped_column(String(32), nullable=True, index=True)
    live_summary_json: Mapped[dict|None]=mapped_column(JSON, nullable=True)
    quick_analysis_json: Mapped[dict|None]=mapped_column(JSON, nullable=True)
    started_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow)
    ended_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)


class ReproductionEventRecord(Base):
    __tablename__='reproduction_events'
    id: Mapped[str]=mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str]=mapped_column(ForeignKey('reproduction_sessions.id', ondelete='CASCADE'), index=True)
    attempt_id: Mapped[str|None]=mapped_column(ForeignKey('reproduction_attempts.id', ondelete='SET NULL'), nullable=True, index=True)
    call_id: Mapped[str|None]=mapped_column(ForeignKey('reproduction_calls.id', ondelete='SET NULL'), nullable=True, index=True)
    case_id: Mapped[str]=mapped_column(ForeignKey('cases.id', ondelete='CASCADE'), index=True)
    event_type: Mapped[str]=mapped_column(String(128), index=True)
    source: Mapped[str]=mapped_column(String(64), index=True)
    anchor_type: Mapped[str|None]=mapped_column(String(64), nullable=True, index=True)
    session_relative_ms: Mapped[int|None]=mapped_column(BigInteger, nullable=True, index=True)
    source_timestamp: Mapped[str|None]=mapped_column(String(128), nullable=True)
    timestamp_source: Mapped[str|None]=mapped_column(String(64), nullable=True)
    uncertainty_ms: Mapped[int|None]=mapped_column(Integer, nullable=True)
    payload_json: Mapped[dict|None]=mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class VoiceRuntimeContextSnapshot(Base):
    __tablename__='voice_runtime_context_snapshots'
    __table_args__=(UniqueConstraint('session_id',name='uq_voice_context_session'),)
    id: Mapped[str]=mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str]=mapped_column(ForeignKey('reproduction_sessions.id', ondelete='CASCADE'), index=True)
    case_id: Mapped[str]=mapped_column(ForeignKey('cases.id', ondelete='CASCADE'), index=True)
    voice_vlan_id: Mapped[str|None]=mapped_column(String(64), nullable=True)
    voice_interface: Mapped[str|None]=mapped_column(String(128), nullable=True)
    voice_device_ip: Mapped[str|None]=mapped_column(String(128), nullable=True)
    voice_gateway_ip: Mapped[str|None]=mapped_column(String(128), nullable=True)
    interface_up: Mapped[bool]=mapped_column(Boolean, default=False)
    resolver_id: Mapped[str]=mapped_column(String(128), default='MOCK_VOICE_CONTEXT_V1')
    resolver_version: Mapped[str]=mapped_column(String(64), default='1.0.0')
    snapshot_json: Mapped[dict]=mapped_column(JSON)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow)


class ArmValidationResult(Base):
    __tablename__='arm_validation_results'
    id: Mapped[str]=mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str]=mapped_column(ForeignKey('reproduction_sessions.id', ondelete='CASCADE'), index=True)
    validation_no: Mapped[int]=mapped_column(Integer, default=1)
    status: Mapped[str]=mapped_column(String(32), index=True, default=ArmValidationStatus.PENDING.value)
    required_channels_json: Mapped[list]=mapped_column(JSON)
    observed_channels_json: Mapped[dict]=mapped_column(JSON)
    failed_reasons_json: Mapped[list|None]=mapped_column(JSON, nullable=True)
    started_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)


class CaptureChannelHealth(Base):
    __tablename__='capture_channel_health'
    __table_args__=(UniqueConstraint('session_id','channel',name='uq_capture_channel_health'),)
    id: Mapped[str]=mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str]=mapped_column(ForeignKey('reproduction_sessions.id', ondelete='CASCADE'), index=True)
    channel: Mapped[str]=mapped_column(String(32), index=True)
    status: Mapped[str]=mapped_column(String(32), index=True, default=ChannelHealth.UNKNOWN.value)
    packet_count: Mapped[int]=mapped_column(BigInteger, default=0)
    last_observed_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)
    health_json: Mapped[dict|None]=mapped_column(JSON, nullable=True)
    updated_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class DeviceDiagnosticLock(Base):
    __tablename__='device_diagnostic_locks'
    __table_args__=(UniqueConstraint('device_id',name='uq_device_diagnostic_lock_device'),)
    id: Mapped[str]=mapped_column(String(36), primary_key=True, default=new_id)
    device_id: Mapped[str]=mapped_column(ForeignKey('case_devices.id', ondelete='CASCADE'), index=True)
    session_id: Mapped[str]=mapped_column(ForeignKey('reproduction_sessions.id', ondelete='CASCADE'), unique=True, index=True)
    owner_worker: Mapped[str]=mapped_column(String(128), index=True)
    status: Mapped[str]=mapped_column(String(32), index=True, default=LockStatus.ACTIVE.value)
    acquired_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow)
    heartbeat_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow)
    lease_expires_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), index=True)
    released_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)


class ReproductionCaptureState(Base):
    __tablename__='reproduction_capture_states'
    __table_args__=(UniqueConstraint('session_id',name='uq_reproduction_capture_state_session'),)
    id: Mapped[str]=mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str]=mapped_column(ForeignKey('reproduction_sessions.id', ondelete='CASCADE'), index=True)
    pretrigger_ms: Mapped[int]=mapped_column(BigInteger, default=30000)
    segment_ms: Mapped[int]=mapped_column(BigInteger, default=5000)
    preserve_mode: Mapped[bool]=mapped_column(Boolean, default=False)
    freeze_anchor_ms: Mapped[int|None]=mapped_column(BigInteger, nullable=True)
    total_bytes: Mapped[int]=mapped_column(BigInteger, default=0)
    finalized: Mapped[bool]=mapped_column(Boolean, default=False)
    manifest_json: Mapped[dict|None]=mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ReproductionCaptureSegment(Base):
    __tablename__='reproduction_capture_segments'
    __table_args__=(UniqueConstraint('session_id','channel','segment_no',name='uq_reproduction_capture_segment_no'),)
    id: Mapped[str]=mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str]=mapped_column(ForeignKey('reproduction_sessions.id', ondelete='CASCADE'), index=True)
    attempt_id: Mapped[str|None]=mapped_column(ForeignKey('reproduction_attempts.id', ondelete='SET NULL'), nullable=True, index=True)
    call_id: Mapped[str|None]=mapped_column(ForeignKey('reproduction_calls.id', ondelete='SET NULL'), nullable=True, index=True)
    evidence_id: Mapped[str|None]=mapped_column(ForeignKey('evidences.id', ondelete='SET NULL'), nullable=True, index=True)
    channel: Mapped[str]=mapped_column(String(32), index=True)
    segment_no: Mapped[int]=mapped_column(Integer)
    start_ms: Mapped[int]=mapped_column(BigInteger, index=True)
    end_ms: Mapped[int]=mapped_column(BigInteger, index=True)
    local_path: Mapped[str]=mapped_column(String(1024))
    content_type: Mapped[str]=mapped_column(String(128))
    size_bytes: Mapped[int]=mapped_column(BigInteger)
    sha256: Mapped[str]=mapped_column(String(64), index=True)
    status: Mapped[str]=mapped_column(String(32), index=True, default='ACTIVE')
    frozen: Mapped[bool]=mapped_column(Boolean, default=False)
    retained: Mapped[bool]=mapped_column(Boolean, default=False)
    retention_class: Mapped[str]=mapped_column(String(32), index=True, default='TEMP_RING')
    metadata_json: Mapped[dict|None]=mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow)


class EvidenceFinalizeRun(Base):
    __tablename__='evidence_finalize_runs'
    __table_args__=(UniqueConstraint('session_id','run_no',name='uq_evidence_finalize_run_no'),)
    id: Mapped[str]=mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str]=mapped_column(ForeignKey('reproduction_sessions.id', ondelete='CASCADE'), index=True)
    run_no: Mapped[int]=mapped_column(Integer)
    status: Mapped[str]=mapped_column(String(32), index=True, default='PENDING')
    evidence_ids_json: Mapped[list|None]=mapped_column(JSON, nullable=True)
    manifest_object_key: Mapped[str|None]=mapped_column(String(1024), nullable=True)
    manifest_sha256: Mapped[str|None]=mapped_column(String(64), nullable=True)
    error_code: Mapped[str|None]=mapped_column(String(128), nullable=True)
    error_message: Mapped[str|None]=mapped_column(Text, nullable=True)
    started_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow)


class CleanupRun(Base):
    __tablename__='cleanup_runs'
    id: Mapped[str]=mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str]=mapped_column(ForeignKey('reproduction_sessions.id', ondelete='CASCADE'), index=True)
    run_no: Mapped[int]=mapped_column(Integer, default=1)
    status: Mapped[str]=mapped_column(String(32), index=True, default=CleanupRunStatus.PENDING.value)
    action_results_json: Mapped[dict|None]=mapped_column(JSON, nullable=True)
    validation_json: Mapped[dict|None]=mapped_column(JSON, nullable=True)
    error_code: Mapped[str|None]=mapped_column(String(128), nullable=True)
    started_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow)


class DiagnosticQuestion(Base):
    __tablename__='diagnostic_questions'
    id: Mapped[str]=mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str]=mapped_column(ForeignKey('cases.id', ondelete='CASCADE'), index=True)
    session_id: Mapped[str|None]=mapped_column(ForeignKey('reproduction_sessions.id', ondelete='SET NULL'), nullable=True, index=True)
    parent_question_id: Mapped[str|None]=mapped_column(ForeignKey('diagnostic_questions.id', ondelete='SET NULL'), nullable=True, index=True)
    question_key: Mapped[str]=mapped_column(String(128), index=True)
    template_version: Mapped[str]=mapped_column(String(64), default='1.0.0')
    template_checksum: Mapped[str|None]=mapped_column(String(64), nullable=True, index=True)
    state: Mapped[str]=mapped_column(String(32), index=True, default=DiagnosticQuestionState.OPEN.value)
    level: Mapped[str]=mapped_column(String(32), default='SYMPTOM_CONFIRM', index=True)
    priority: Mapped[int]=mapped_column(Integer, default=100)
    information_gain: Mapped[int]=mapped_column(Integer, default=1000)
    selected_reason: Mapped[str|None]=mapped_column(Text, nullable=True)
    requirements_json: Mapped[dict]=mapped_column(JSON)
    answer_json: Mapped[dict|None]=mapped_column(JSON, nullable=True)
    evidence_refs_json: Mapped[list|None]=mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class DiagnosticExperiment(Base):
    __tablename__='diagnostic_experiments'
    id: Mapped[str]=mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str]=mapped_column(ForeignKey('cases.id', ondelete='CASCADE'), index=True)
    hypothesis_id: Mapped[str|None]=mapped_column(ForeignKey('hypotheses.id', ondelete='SET NULL'), nullable=True, index=True)
    question_id: Mapped[str|None]=mapped_column(ForeignKey('diagnostic_questions.id', ondelete='SET NULL'), nullable=True, index=True)
    profile_key: Mapped[str]=mapped_column(String(128), index=True)
    profile_version: Mapped[str]=mapped_column(String(64))
    profile_checksum: Mapped[str]=mapped_column(String(64), index=True)
    effective_profile_snapshot: Mapped[dict]=mapped_column(JSON)
    state: Mapped[str]=mapped_column(String(40), index=True, default=ExperimentState.CREATED.value)
    confirmation_policy: Mapped[str]=mapped_column(String(40), index=True)
    independent_variable: Mapped[str]=mapped_column(String(128), index=True)
    target_finding: Mapped[str]=mapped_column(String(128), index=True)
    reproduction_profile_id: Mapped[str]=mapped_column(String(128), index=True)
    current_round: Mapped[int]=mapped_column(Integer, default=0)
    causal_state: Mapped[str]=mapped_column(String(40), index=True, default=CausalConclusionState.HYPOTHESIS.value)
    terminal_reason: Mapped[str|None]=mapped_column(String(128), nullable=True)
    created_by: Mapped[str|None]=mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ExperimentRun(Base):
    __tablename__='experiment_runs'
    __table_args__=(UniqueConstraint('experiment_id','run_no',name='uq_experiment_run_no'),)
    id: Mapped[str]=mapped_column(String(36), primary_key=True, default=new_id)
    experiment_id: Mapped[str]=mapped_column(ForeignKey('diagnostic_experiments.id', ondelete='CASCADE'), index=True)
    case_id: Mapped[str]=mapped_column(ForeignKey('cases.id', ondelete='CASCADE'), index=True)
    run_no: Mapped[int]=mapped_column(Integer)
    variant: Mapped[str]=mapped_column(String(16), index=True)
    status: Mapped[str]=mapped_column(String(40), index=True, default=ExperimentRunStatus.PLANNED.value)
    reproduction_session_id: Mapped[str|None]=mapped_column(ForeignKey('reproduction_sessions.id', ondelete='SET NULL'), nullable=True, index=True)
    reproduction_call_id: Mapped[str|None]=mapped_column(ForeignKey('reproduction_calls.id', ondelete='SET NULL'), nullable=True, index=True)
    target_verdict: Mapped[str|None]=mapped_column(String(32), nullable=True, index=True)
    target_finding_present: Mapped[bool|None]=mapped_column(Boolean, nullable=True)
    metrics_json: Mapped[dict|None]=mapped_column(JSON, nullable=True)
    external_action_required: Mapped[bool]=mapped_column(Boolean, default=True)
    external_action_completed_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow)


class ExperimentEnvironmentSnapshot(Base):
    __tablename__='experiment_environment_snapshots'
    id: Mapped[str]=mapped_column(String(36), primary_key=True, default=new_id)
    experiment_id: Mapped[str]=mapped_column(ForeignKey('diagnostic_experiments.id', ondelete='CASCADE'), index=True)
    run_id: Mapped[str]=mapped_column(ForeignKey('experiment_runs.id', ondelete='CASCADE'), index=True)
    case_id: Mapped[str]=mapped_column(ForeignKey('cases.id', ondelete='CASCADE'), index=True)
    phase: Mapped[str]=mapped_column(String(16), default='PRE', index=True)
    snapshot_json: Mapped[dict]=mapped_column(JSON)
    checksum: Mapped[str]=mapped_column(String(64), index=True)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow)


class EnvironmentComparison(Base):
    __tablename__='environment_comparisons'
    id: Mapped[str]=mapped_column(String(36), primary_key=True, default=new_id)
    experiment_id: Mapped[str]=mapped_column(ForeignKey('diagnostic_experiments.id', ondelete='CASCADE'), index=True)
    baseline_run_id: Mapped[str]=mapped_column(ForeignKey('experiment_runs.id', ondelete='CASCADE'), index=True)
    variant_run_id: Mapped[str]=mapped_column(ForeignKey('experiment_runs.id', ondelete='CASCADE'), index=True)
    status: Mapped[str]=mapped_column(String(40), index=True, default=EnvironmentComparisonStatus.COMPARABLE.value)
    expected_changes_json: Mapped[list|None]=mapped_column(JSON, nullable=True)
    soft_drift_json: Mapped[list|None]=mapped_column(JSON, nullable=True)
    hard_drift_json: Mapped[list|None]=mapped_column(JSON, nullable=True)
    compared_fields_json: Mapped[dict|None]=mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow)


class CausalAssessment(Base):
    __tablename__='causal_assessments'
    id: Mapped[str]=mapped_column(String(36), primary_key=True, default=new_id)
    experiment_id: Mapped[str]=mapped_column(ForeignKey('diagnostic_experiments.id', ondelete='CASCADE'), index=True)
    case_id: Mapped[str]=mapped_column(ForeignKey('cases.id', ondelete='CASCADE'), index=True)
    hypothesis_id: Mapped[str|None]=mapped_column(ForeignKey('hypotheses.id', ondelete='SET NULL'), nullable=True, index=True)
    state: Mapped[str]=mapped_column(String(40), index=True, default=CausalConclusionState.HYPOTHESIS.value)
    confirmation_policy: Mapped[str]=mapped_column(String(40), index=True)
    supporting_run_ids_json: Mapped[list]=mapped_column(JSON)
    environment_comparison_ids_json: Mapped[list]=mapped_column(JSON)
    hard_contradictions_json: Mapped[list|None]=mapped_column(JSON, nullable=True)
    rationale_json: Mapped[dict]=mapped_column(JSON)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow)


class FixAction(Base):
    __tablename__='fix_actions'
    id: Mapped[str]=mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str]=mapped_column(ForeignKey('cases.id', ondelete='CASCADE'), index=True)
    hypothesis_id: Mapped[str|None]=mapped_column(ForeignKey('hypotheses.id', ondelete='SET NULL'), nullable=True, index=True)
    experiment_id: Mapped[str|None]=mapped_column(ForeignKey('diagnostic_experiments.id', ondelete='SET NULL'), nullable=True, index=True)
    action_type: Mapped[str]=mapped_column(String(64), index=True, default=FixActionType.OTHER.value)
    description: Mapped[str]=mapped_column(Text)
    version_before: Mapped[str|None]=mapped_column(String(128), nullable=True)
    version_after: Mapped[str|None]=mapped_column(String(128), nullable=True)
    actor: Mapped[str|None]=mapped_column(String(128), nullable=True)
    metadata_json: Mapped[dict|None]=mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow)


class FixVerificationRun(Base):
    __tablename__='fix_verification_runs'
    id: Mapped[str]=mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str]=mapped_column(ForeignKey('cases.id', ondelete='CASCADE'), index=True)
    fix_action_id: Mapped[str]=mapped_column(ForeignKey('fix_actions.id', ondelete='CASCADE'), index=True)
    baseline_session_id: Mapped[str|None]=mapped_column(ForeignKey('reproduction_sessions.id', ondelete='SET NULL'), nullable=True, index=True)
    verification_session_id: Mapped[str|None]=mapped_column(ForeignKey('reproduction_sessions.id', ondelete='SET NULL'), nullable=True, index=True)
    baseline_call_id: Mapped[str|None]=mapped_column(ForeignKey('reproduction_calls.id', ondelete='SET NULL'), nullable=True, index=True)
    verification_call_id: Mapped[str|None]=mapped_column(ForeignKey('reproduction_calls.id', ondelete='SET NULL'), nullable=True, index=True)
    reproduction_profile_id: Mapped[str]=mapped_column(String(128), index=True)
    target_finding: Mapped[str]=mapped_column(String(128), index=True)
    required_calls: Mapped[int]=mapped_column(Integer, default=1)
    max_calls: Mapped[int]=mapped_column(Integer, default=3)
    verification_call_count: Mapped[int]=mapped_column(Integer, default=0)
    successful_call_count: Mapped[int]=mapped_column(Integer, default=0)
    evaluations_json: Mapped[list|None]=mapped_column(JSON, nullable=True)
    status: Mapped[str]=mapped_column(String(40), index=True, default=FixVerificationStatus.PENDING.value)
    environment_status: Mapped[str|None]=mapped_column(String(40), nullable=True)
    business_checks_json: Mapped[dict|None]=mapped_column(JSON, nullable=True)
    comparison_json: Mapped[dict|None]=mapped_column(JSON, nullable=True)
    evidence_id: Mapped[str|None]=mapped_column(ForeignKey('evidences.id', ondelete='SET NULL'), nullable=True, index=True)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
