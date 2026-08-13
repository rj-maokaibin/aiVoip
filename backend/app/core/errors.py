from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.contracts.enums import ErrorCategory


@dataclass(frozen=True)
class ErrorDefinition:
    code: str
    category: ErrorCategory
    retryable: bool = False
    http_status: int = 400
    default_message: str = "Request failed"


_ERROR_DEFS = [
    ErrorDefinition("REQUEST_VALIDATION_FAILED", ErrorCategory.VALIDATION, False, 422, "Request validation failed"),
    ErrorDefinition("ROUTE_NOT_FOUND", ErrorCategory.VALIDATION, False, 404, "Route was not found"),
    ErrorDefinition("CASE_NOT_FOUND", ErrorCategory.VALIDATION, False, 404, "Case was not found"),
    ErrorDefinition("JOB_NOT_FOUND", ErrorCategory.VALIDATION, False, 404, "Job was not found"),
    ErrorDefinition("EVIDENCE_NOT_FOUND", ErrorCategory.VALIDATION, False, 404, "Evidence was not found"),
    ErrorDefinition("EVIDENCE_LINEAGE_REQUIRED", ErrorCategory.VALIDATION, False, 422, "Derived evidence requires parent evidence"),
    ErrorDefinition("EVIDENCE_PARENT_INVALID", ErrorCategory.VALIDATION, False, 422, "Parent evidence is missing or belongs to another case"),
    ErrorDefinition("ARTIFACT_NOT_FOUND", ErrorCategory.VALIDATION, False, 404, "Artifact was not found"),
    ErrorDefinition("ANALYZER_RUN_NOT_FOUND", ErrorCategory.ANALYZER, False, 404, "Analyzer run was not found"),
    ErrorDefinition("ANALYZER_RESULT_NOT_READY", ErrorCategory.ANALYZER, True, 409, "Analyzer result is not ready"),
    ErrorDefinition("DIAGNOSIS_NOT_FOUND", ErrorCategory.VALIDATION, False, 404, "Diagnosis was not found"),
    ErrorDefinition("HYPOTHESIS_NOT_FOUND", ErrorCategory.VALIDATION, False, 404, "Hypothesis was not found"),
    ErrorDefinition("HYPOTHESIS_NOT_CONFIRMABLE", ErrorCategory.DIAGNOSIS, False, 409, "Hypothesis is not confirmable"),
    ErrorDefinition("CONFIRM_RULE_REQUIRED", ErrorCategory.DIAGNOSIS, False, 409, "A confirm rule is required"),
    ErrorDefinition("DIRECT_EVIDENCE_REQUIRED", ErrorCategory.DIAGNOSIS, False, 409, "Direct evidence is required"),
    ErrorDefinition("KEY_CONTRADICTION_EXISTS", ErrorCategory.DIAGNOSIS, False, 409, "A blocking contradiction exists"),
    ErrorDefinition("CASE_TRANSITION_NOT_ALLOWED", ErrorCategory.VALIDATION, False, 409, "Case transition is not allowed"),
    ErrorDefinition("JOB_TRANSITION_NOT_ALLOWED", ErrorCategory.VALIDATION, False, 409, "Job transition is not allowed"),
    ErrorDefinition("FIX_VERIFICATION_EVIDENCE_REQUIRED", ErrorCategory.DIAGNOSIS, False, 409, "Fix verification evidence is required"),
    ErrorDefinition("INVALID_CURSOR", ErrorCategory.VALIDATION, False, 400, "Cursor is invalid"),
    ErrorDefinition("IDEMPOTENCY_KEY_REUSED", ErrorCategory.VALIDATION, False, 409, "Idempotency key was reused with a different request"),
    ErrorDefinition("IDEMPOTENCY_IN_PROGRESS", ErrorCategory.VALIDATION, True, 409, "An equivalent request is still in progress"),
    ErrorDefinition("AUTH_REQUIRED", ErrorCategory.AUTH, False, 401, "Authentication is required"),
    ErrorDefinition("AUTH_PROVIDER_NOT_CONFIGURED", ErrorCategory.AUTH, False, 503, "Production authentication provider is not configured"),
    ErrorDefinition("AUTH_SIGNATURE_INVALID", ErrorCategory.AUTH, False, 401, "Authentication signature is invalid"),
    ErrorDefinition("AUTH_SIGNATURE_EXPIRED", ErrorCategory.AUTH, False, 401, "Authentication signature has expired"),
    ErrorDefinition("FEISHU_TRANSPORT_NOT_CONFIGURED", ErrorCategory.INTERNAL, False, 503, "Feishu live transport is not configured"),
    ErrorDefinition("FEISHU_CALLBACK_INVALID", ErrorCategory.AUTH, False, 401, "Feishu callback verification failed"),
    ErrorDefinition("INVALID_ROLE", ErrorCategory.AUTH, False, 401, "Unknown role"),
    ErrorDefinition("PERMISSION_DENIED", ErrorCategory.PERMISSION, False, 403, "Permission denied"),
    ErrorDefinition("RULE_NOT_FOUND", ErrorCategory.VALIDATION, False, 404, "Rule was not found"),
    ErrorDefinition("RULE_VERSION_NOT_FOUND", ErrorCategory.VALIDATION, False, 404, "Rule version was not found"),
    ErrorDefinition("RULE_ACTIVATION_REQUIRES_SEPARATE_APPROVAL", ErrorCategory.PERMISSION, False, 409, "Rule activation requires separate approval"),
    ErrorDefinition("KNOWLEDGE_NOT_FOUND", ErrorCategory.VALIDATION, False, 404, "Knowledge item was not found"),
    ErrorDefinition("KNOWLEDGE_SELF_REVIEW_NOT_ALLOWED", ErrorCategory.PERMISSION, False, 409, "Creator cannot self-approve knowledge"),
    ErrorDefinition("REPORT_NOT_FOUND", ErrorCategory.VALIDATION, False, 404, "Report was not found"),
    ErrorDefinition("ACTION_NOT_ALLOWED", ErrorCategory.ACTION, False, 403, "Action is not allowed"),
    ErrorDefinition("ACTION_RISK_NOT_ALLOWED", ErrorCategory.ACTION, False, 403, "Action risk level is not allowed"),
    ErrorDefinition("EVIDENCE_PURGE_FORBIDDEN", ErrorCategory.PERMISSION, False, 403, "Raw evidence cannot be deleted by this operation"),
    ErrorDefinition("CANCEL_CLEANUP_REQUIRED", ErrorCategory.CLEANUP, True, 409, "Cleanup must complete before cancellation is final"),
    ErrorDefinition("DEPENDENCY_NOT_SATISFIED", ErrorCategory.VALIDATION, True, 409, "Job dependency is not satisfied"),
    ErrorDefinition("JOB_DEPENDENCY_CYCLE", ErrorCategory.VALIDATION, False, 409, "Job dependency would create a cycle"),
    ErrorDefinition("JOB_DEPENDENCY_CROSS_CASE", ErrorCategory.VALIDATION, False, 409, "Job dependencies must remain within one case"),
    ErrorDefinition("JOB_DEPENDENCY_JOB_NOT_PENDING", ErrorCategory.VALIDATION, False, 409, "Dependencies can only be changed while the job is pending"),
    ErrorDefinition("JOB_DEPENDENCY_CONFLICT", ErrorCategory.VALIDATION, False, 409, "Dependency already exists with a different policy"),
    ErrorDefinition("REPRODUCTION_NOT_FOUND", ErrorCategory.REPRODUCTION, False, 404, "Reproduction session was not found"),
    ErrorDefinition("REPRODUCTION_PROFILE_NOT_FOUND", ErrorCategory.REPRODUCTION, False, 404, "Reproduction profile was not found"),
    ErrorDefinition("REPRODUCTION_PLATFORM_NOT_CONFIGURED", ErrorCategory.REPRODUCTION, False, 503, "Production reproduction platform is not configured"),
    ErrorDefinition("REPRODUCTION_TRANSITION_NOT_ALLOWED", ErrorCategory.REPRODUCTION, False, 409, "Reproduction transition is not allowed"),
    ErrorDefinition("DEVICE_DIAGNOSTIC_LOCKED", ErrorCategory.REPRODUCTION, True, 409, "Device is already owned by an active reproduction session"),
    ErrorDefinition("VOICE_CONTEXT_NOT_FOUND", ErrorCategory.VOICE_CONTEXT, False, 409, "Voice runtime context was not found"),
    ErrorDefinition("VOICE_CONTEXT_INVALID", ErrorCategory.VOICE_CONTEXT, False, 409, "Voice runtime context is invalid"),
    ErrorDefinition("VOICE_INTERFACE_MISSING", ErrorCategory.VOICE_CONTEXT, False, 409, "Voice interface is missing"),
    ErrorDefinition("VOICE_GATEWAY_CONFIG_INVALID", ErrorCategory.VOICE_CONTEXT, False, 409, "Voice gateway configuration is invalid"),
    ErrorDefinition("ARM_NOT_READY", ErrorCategory.CAPTURE, True, 409, "ARM readiness barrier was not satisfied"),
    ErrorDefinition("PCM_RX_NOT_READY", ErrorCategory.PCM, True, 409, "PCM RX stream was not observed"),
    ErrorDefinition("PCM_TX_NOT_READY", ErrorCategory.PCM, True, 409, "PCM TX stream was not observed"),
    ErrorDefinition("PCAP_STREAM_NOT_READY", ErrorCategory.CAPTURE, True, 409, "PCAP stream is not ready"),
    ErrorDefinition("DEBUG_CHANNEL_NOT_READY", ErrorCategory.CAPTURE, True, 409, "Debug channel is not ready"),
    ErrorDefinition("CLEANUP_VERIFICATION_FAILED", ErrorCategory.CLEANUP, True, 409, "Cleanup reverse validation failed"),
    ErrorDefinition("REPRODUCTION_LEASE_EXPIRED", ErrorCategory.REPRODUCTION, True, 409, "Reproduction worker lease expired"),
    ErrorDefinition("REPRODUCTION_CALL_NOT_FOUND", ErrorCategory.REPRODUCTION, False, 404, "Reproduction call was not found"),
    ErrorDefinition("REPRODUCTION_ATTEMPT_NOT_FOUND", ErrorCategory.REPRODUCTION, False, 404, "Reproduction attempt was not found"),
    ErrorDefinition("REPRODUCTION_PROFILE_CONTRACT_INVALID", ErrorCategory.VALIDATION, False, 422, "Reproduction profile contract is invalid"),
    ErrorDefinition("DIAGNOSTIC_QUESTION_TEMPLATE_NOT_FOUND", ErrorCategory.DIAGNOSIS, False, 404, "Diagnostic question template was not found"),
    ErrorDefinition("DIAGNOSTIC_QUESTION_NOT_FOUND", ErrorCategory.DIAGNOSIS, False, 404, "Diagnostic question was not found"),
    ErrorDefinition("DIAGNOSTIC_QUESTION_EVIDENCE_INSUFFICIENT", ErrorCategory.DIAGNOSIS, False, 409, "Diagnostic question evidence requirements are not satisfied"),
    ErrorDefinition("EXPERIMENT_PROFILE_NOT_FOUND", ErrorCategory.DIAGNOSIS, False, 404, "Experiment profile was not found"),
    ErrorDefinition("EXPERIMENT_PROFILE_NOT_APPLICABLE", ErrorCategory.DIAGNOSIS, False, 409, "Experiment profile is not applicable to the current question or hypothesis"),
    ErrorDefinition("EXPERIMENT_NOT_FOUND", ErrorCategory.DIAGNOSIS, False, 404, "Diagnostic experiment was not found"),
    ErrorDefinition("EXPERIMENT_RUN_NOT_FOUND", ErrorCategory.DIAGNOSIS, False, 404, "Experiment run was not found"),
    ErrorDefinition("EXPERIMENT_RUN_TRANSITION_NOT_ALLOWED", ErrorCategory.DIAGNOSIS, False, 409, "Experiment run transition is not allowed"),
    ErrorDefinition("EXPERIMENT_BASELINE_REQUIRED", ErrorCategory.DIAGNOSIS, False, 409, "A completed A1 baseline is required"),
    ErrorDefinition("EXPERIMENT_ENVIRONMENT_SNAPSHOT_REQUIRED", ErrorCategory.DIAGNOSIS, False, 409, "Experiment environment snapshot is required"),
    ErrorDefinition("EXPERIMENT_REPRODUCTION_NOT_TERMINAL", ErrorCategory.DIAGNOSIS, True, 409, "Reproduction session is not ready for experiment evaluation"),
    ErrorDefinition("ROOT_CAUSE_CONFIRMATION_REQUIRED", ErrorCategory.DIAGNOSIS, False, 409, "Root cause confirmation is required before recording a fix"),
    ErrorDefinition("FIX_ACTION_NOT_FOUND", ErrorCategory.DIAGNOSIS, False, 404, "Fix action was not found"),
    ErrorDefinition("FIX_VERIFICATION_NOT_FOUND", ErrorCategory.DIAGNOSIS, False, 404, "Fix verification was not found"),
    ErrorDefinition("FIX_VERIFICATION_TERMINAL", ErrorCategory.DIAGNOSIS, False, 409, "Fix verification is already terminal"),
    ErrorDefinition("FIX_BASELINE_TARGET_REQUIRED", ErrorCategory.DIAGNOSIS, False, 409, "Baseline call must contain the target finding"),
    ErrorDefinition("INTERNAL_ERROR", ErrorCategory.INTERNAL, True, 500, "Internal server error"),
]

ERROR_REGISTRY: dict[str, ErrorDefinition] = {x.code: x for x in _ERROR_DEFS}


def error_definition(code: str, *, http_status: int | None = None) -> ErrorDefinition:
    item = ERROR_REGISTRY.get(code)
    if item:
        return item
    return ErrorDefinition(
        code=code,
        category=ErrorCategory.INTERNAL if (http_status or 500) >= 500 else ErrorCategory.VALIDATION,
        retryable=(http_status or 500) >= 500,
        http_status=http_status or 400,
        default_message=code.replace("_", " ").title(),
    )


class AppError(Exception):
    def __init__(
        self,
        code: str,
        *,
        message: str | None = None,
        details: dict[str, Any] | None = None,
        http_status: int | None = None,
        retryable: bool | None = None,
        category: ErrorCategory | None = None,
    ):
        definition = error_definition(code, http_status=http_status)
        self.code = code
        self.message = message or definition.default_message
        self.details = details or {}
        self.http_status = http_status or definition.http_status
        self.retryable = definition.retryable if retryable is None else retryable
        self.category = category or definition.category
        super().__init__(self.message)

    def as_payload(self, trace_id: str | None = None) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "retryable": bool(self.retryable),
                "category": self.category.value,
                "details": self.details,
                "trace_id": trace_id,
            }
        }
