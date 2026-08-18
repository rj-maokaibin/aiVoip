from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    app_env: str = "development"
    app_version: str = "1.0.0-rc1-f3"
    build_revision: str = "dev"
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    database_url: str = "postgresql+psycopg://voip:voip@postgres:5432/voip"
    redis_url: str = "redis://redis:6379/0"
    minio_endpoint: str = "minio:9000"
    minio_access_key: str = "voipminio"
    minio_access_key_file: str = ""
    minio_access_key_env: str = ""
    minio_secret_key: str = "voipminiosecret"
    minio_secret_key_file: str = ""
    minio_secret_key_env: str = ""
    minio_secure: bool = False
    minio_bucket: str = "voip-evidence"
    artifact_url_ttl_minutes: int = 15
    credential_provider: str = "mock"
    mock_device_password: str = "change-me"
    credential_api_url: str = ""
    credential_api_token: str = ""
    credential_api_token_file: str = ""
    credential_api_token_env: str = ""
    ssh_username: str = "admin"
    ssh_connect_timeout: float = 8.0
    ssh_command_timeout: float = 10.0
    aim_prompt: str = "AIM>"
    log_level: str = "INFO"
    tshark_binary: str = "tshark"
    tshark_timeout_seconds: int = 300
    profile_root: Path = Path("/app/profiles")
    rule_root: Path = Path("/app/rules/diagnosis")
    knowledge_root: Path = Path("/app/knowledge/seed")
    knowledge_similarity_min_score: float = 0.18
    knowledge_similarity_limit: int = 5
    reasoning_prompt_version: str = "voip-diagnosis-v2"
    reasoning_gateway_include_device_identifiers: bool = False
    diagnosis_no_progress_limit: int = 2
    diagnosis_max_cycles: int = 6
    diagnosis_reasoner: str = "deterministic"
    reproduction_platform_mode: str = "mock"
    reproduction_capture_root: Path = Path("/tmp/voip-reproduction-capture")
    reproduction_object_root: Path = Path("/tmp/voip-reproduction-objects")
    reproduction_storage_mode: str = "local"
    reasoning_gateway_url: str = ""
    reasoning_gateway_token: str = ""
    reasoning_gateway_model: str = ""
    reasoning_gateway_timeout_seconds: float = 20.0
    ai_shadow_enabled: bool = False
    ai_shadow_workflow_version: str = "ai-shadow-v2"
    ai_promotion_stage: str = "OFF"
    ai_promotion_gate_passed: bool = False
    ai_allow_manual_promotion_override: bool = False
    ai_promotion_gate_artifact: Path = Path("/app/validation/ai_promotion_gate.json")
    ai_eval_min_samples: int = 10
    ai_eval_min_top1_recall: float = 0.60
    ai_eval_min_top3_recall: float = 0.80
    ai_eval_min_fault_domain_recall: float = 0.80
    ai_eval_min_evidence_precision: float = 0.98
    ai_eval_max_unsupported_claim_rate: float = 0.05
    ai_eval_max_unauthorized_suggestion_rate: float = 0.0
    reasoning_gateway_models: str = ""
    reasoning_gateway_failover_enabled: bool = True
    # AI1 is released SHADOW-first. Even when enabled, V1 records/evaluates the
    # semantic proposal while the deterministic router remains execution authority.
    ai_semantic_router_enabled: bool = False
    ai_semantic_router_mode: str = "SHADOW"
    ai_semantic_router_min_confidence: float = 0.80
    # AI3 is read-only. Enabling it permits grounded Case Q&A only; it never
    # enables device/reproduction/experiment/fix execution.
    ai_case_copilot_enabled: bool = False
    # AI2 V1 exposes only SHADOW/SUGGEST software behavior. CONTROLLED_PLANNER
    # remains guarded by the existing machine-generated promotion artifact and the
    # deterministic Policy/Orchestrator path.
    ai_diagnostic_loop_enabled: bool = False
    ai_diagnostic_loop_workflow_version: str = "ai-diagnostic-loop-v1"
    auth_allow_anonymous_dev: bool = True
    production_auth_provider: str = "pending"
    auth_gateway_hmac_secret: str = ""
    auth_gateway_hmac_secret_file: str = ""
    auth_gateway_hmac_secret_env: str = ""
    auth_gateway_max_skew_seconds: int = 300
    cors_allow_origins: str = "*"
    feishu_live_enabled: bool = False
    feishu_base_url: str = "https://open.feishu.cn/open-apis"
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_app_secret_file: str = ""
    feishu_app_secret_env: str = ""
    feishu_receive_id_type: str = "chat_id"
    feishu_default_receive_id: str = ""
    feishu_encrypt_key: str = ""
    feishu_encrypt_key_file: str = ""
    feishu_encrypt_key_env: str = ""
    feishu_verification_token: str = ""
    feishu_verification_token_file: str = ""
    feishu_verification_token_env: str = ""
    feishu_timeout_seconds: float = 8.0
    feishu_attachment_max_bytes: int = 100 * 1024 * 1024
    feishu_identity_rbac_enabled: bool = False
    feishu_identity_discover_unmapped: bool = True
    feishu_document_acl_enabled: bool = False
    feishu_document_acl_mode: str = "AUTO"
    feishu_document_acl_permission: str = "view"
    feishu_document_acl_fallback_enabled: bool = True
    auth_default_actor: str = "dev-admin"
    auth_default_role: str = "ADMIN"
    idempotency_ttl_hours: int = 24
    sse_poll_interval_seconds: float = 0.75
    sse_batch_size: int = 200

    preliminary_evidence_report_enabled: bool = True
    evidence_retention_raw_days: int = 90
    evidence_retention_batch_size: int = 200
    evidence_retention_worker_enabled: bool = True
    evidence_report_metrics_window_days: int = 30
    evidence_report_basic_sla_seconds: float = 10.0
    evidence_report_full_p95_seconds: float = 30.0
    evidence_report_large_p95_seconds: float = 60.0
    evidence_report_golden_min_recall: float = 0.95
    evidence_report_golden_min_precision: float = 0.95
    evidence_report_boundary_min_correctness: float = 0.95
    evidence_report_boundary_max_wrong_rate: float = 0.01

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    def model_post_init(self, __context) -> None:
        if str(self.ai_promotion_stage or "OFF").upper() != "OFF":
            self.ai_shadow_enabled = True
        semantic_mode = str(self.ai_semantic_router_mode or "SHADOW").upper()
        if semantic_mode not in {"OFF", "SHADOW"}:
            raise ValueError("AI_SEMANTIC_ROUTER_MODE_INVALID")
        self.ai_semantic_router_mode = semantic_mode
        if not 0.0 <= float(self.ai_semantic_router_min_confidence) <= 1.0:
            raise ValueError("AI_SEMANTIC_ROUTER_MIN_CONFIDENCE_INVALID")
        mode = str(self.feishu_document_acl_mode or "AUTO").upper()
        if mode not in {"AUTO", "CHAT_SCOPE", "MEMBER_MIRROR"}:
            raise ValueError("FEISHU_DOCUMENT_ACL_MODE_INVALID")
        self.feishu_document_acl_mode = mode
        perm = str(self.feishu_document_acl_permission or "view").lower()
        if perm not in {"view", "edit", "full_access"}:
            raise ValueError("FEISHU_DOCUMENT_ACL_PERMISSION_INVALID")
        self.feishu_document_acl_permission = perm

settings = Settings()