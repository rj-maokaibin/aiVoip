from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.contracts.enums import (
    CallVerdict, CaseEvent, CaseStatus, DiagnosticQuestionState, EnvironmentComparisonStatus, EventType,
    EvidenceCompleteness, EvidenceKind, EvidenceLevel, EvidenceScope,
    FixActionType, FixVerificationStatus, HypothesisState,
)
from app.core.errors import AppError
from app.db.models import (
    Case, DiagnosticExperiment, DiagnosticQuestion, Evidence, FixAction, FixVerificationRun, Hypothesis,
    ReproductionCall, ReproductionSession,
)
from app.integrations.storage import FilesystemObjectStorage, ObjectStorage, reproduction_object_storage
from app.services.case_transitions import CaseTransitionService
from app.services.evidence import create_evidence
from app.services.events import emit_event
from app.services.audit import audit
from app.reproduction.question_graph import DiagnosticQuestionGraph


def _utcnow():
    return datetime.now(timezone.utc)


def _path_get(data: dict[str, Any], path: str) -> tuple[bool, Any]:
    cur: Any = data
    for token in path.split("."):
        if not isinstance(cur, dict) or token not in cur:
            return False, None
        cur = cur[token]
    return True, cur


class FixVerificationService:
    version = "1.0.0"

    def __init__(self, *, storage: ObjectStorage | FilesystemObjectStorage | None = None):
        self.storage = storage or reproduction_object_storage()

    def create_fix_action(
        self,
        db: Session,
        *,
        case_id: str,
        action_type: FixActionType | str,
        description: str,
        hypothesis_id: str | None = None,
        experiment_id: str | None = None,
        version_before: str | None = None,
        version_after: str | None = None,
        metadata: dict[str, Any] | None = None,
        actor: str | None = None,
    ) -> FixAction:
        case = db.get(Case, case_id)
        if not case:
            raise AppError("CASE_NOT_FOUND")
        hypothesis = db.get(Hypothesis, hypothesis_id) if hypothesis_id else None
        experiment = db.get(DiagnosticExperiment, experiment_id) if experiment_id else None
        root_confirmed = bool(hypothesis and hypothesis.case_id == case_id and hypothesis.status == HypothesisState.CONFIRMED.value)
        root_confirmed = root_confirmed or bool(experiment and experiment.case_id == case_id and experiment.causal_state == "ROOT_CAUSE_CONFIRMED")
        if not root_confirmed or case.status != CaseStatus.ROOT_CAUSE_CONFIRMED.value:
            raise AppError("ROOT_CAUSE_CONFIRMATION_REQUIRED")
        row = FixAction(
            case_id=case_id,
            hypothesis_id=hypothesis_id,
            experiment_id=experiment_id,
            action_type=FixActionType(action_type).value,
            description=description,
            version_before=version_before,
            version_after=version_after,
            actor=actor,
            metadata_json=metadata or {},
        )
        db.add(row)
        db.flush()
        CaseTransitionService.transition(db, case, CaseEvent.RESOLUTION_STARTED, reason="fix action recorded", actor=actor, context={"fix_action_id": row.id})
        audit(db,case_id=case_id,actor=actor,event_type=EventType.FIX_ACTION_CREATED.value,action="FIX_ACTION_CREATE",target_type="fix_action",target_id=row.id,detail={"action_type":row.action_type,"hypothesis_id":hypothesis_id,"experiment_id":experiment_id})
        emit_event(db,event_type=EventType.FIX_ACTION_CREATED,case_id=case_id,entity_type="fix_action",entity_id=row.id,payload={"action_type":row.action_type})
        return row

    def create_verification(
        self,
        db: Session,
        *,
        fix_action_id: str,
        baseline_session_id: str,
        baseline_call_id: str,
        target_finding: str,
        reproduction_profile_id: str | None = None,
        required_calls: int = 1,
        max_calls: int = 3,
    ) -> FixVerificationRun:
        fix = db.get(FixAction, fix_action_id)
        if not fix:
            raise AppError("FIX_ACTION_NOT_FOUND")
        session = db.get(ReproductionSession, baseline_session_id)
        call = db.get(ReproductionCall, baseline_call_id)
        if not session or session.case_id != fix.case_id:
            raise AppError("REPRODUCTION_NOT_FOUND")
        if not call or call.session_id != session.id:
            raise AppError("REPRODUCTION_CALL_NOT_FOUND")
        findings = set((call.quick_analysis_json or {}).get("findings") or [])
        if target_finding not in findings:
            raise AppError("FIX_BASELINE_TARGET_REQUIRED", details={"target_finding": target_finding})
        row = FixVerificationRun(
            case_id=fix.case_id,
            fix_action_id=fix.id,
            baseline_session_id=session.id,
            baseline_call_id=call.id,
            reproduction_profile_id=reproduction_profile_id or session.profile_key,
            target_finding=target_finding,
            required_calls=max(1, int(required_calls)),
            max_calls=max(max(1, int(required_calls)), int(max_calls)),
            status=FixVerificationStatus.PENDING.value,
        )
        db.add(row)
        db.flush()
        emit_event(db,event_type=EventType.FIX_VERIFICATION_UPDATED,case_id=fix.case_id,entity_type="fix_verification",entity_id=row.id,payload={"status":row.status,"target_finding":row.target_finding})
        return row

    @staticmethod
    def compare_environment(
        *,
        baseline: dict[str, Any],
        verification: dict[str, Any],
        allowed_change_paths: list[str] | None = None,
    ) -> tuple[EnvironmentComparisonStatus, list[dict[str, Any]]]:
        allowed = set(allowed_change_paths or [])
        controlled = [
            "device.serial", "software.version", "voice.voice_vlan_id", "voice.gateway_ip",
            "voice.fxs_port", "call.codec", "call.called_number",
        ]
        hard: list[dict[str, Any]] = []
        for path in controlled:
            if path in allowed:
                continue
            a_ok, a = _path_get(baseline, path)
            b_ok, b = _path_get(verification, path)
            if not (a_ok and b_ok):
                hard.append({"path": path, "reason": "CONTROL_FIELD_UNAVAILABLE", "baseline": a, "verification": b})
            elif a != b:
                hard.append({"path": path, "reason": "CONTROL_VARIABLE_CHANGED", "baseline": a, "verification": b})
        return (EnvironmentComparisonStatus.NOT_COMPARABLE if hard else EnvironmentComparisonStatus.COMPARABLE), hard

    def evaluate(
        self,
        db: Session,
        *,
        verification: FixVerificationRun,
        verification_session_id: str,
        verification_call_id: str,
        baseline_environment: dict[str, Any],
        verification_environment: dict[str, Any],
        business_checks: dict[str, bool],
        new_blocking_findings: list[str] | None = None,
        actor: str | None = None,
    ) -> FixVerificationRun:
        fix = db.get(FixAction, verification.fix_action_id)
        if not fix:
            raise AppError("FIX_ACTION_NOT_FOUND")
        baseline_call = db.get(ReproductionCall, verification.baseline_call_id)
        current_session = db.get(ReproductionSession, verification_session_id)
        current_call = db.get(ReproductionCall, verification_call_id)
        if not baseline_call or not current_call or not current_session or current_call.session_id != current_session.id or current_session.case_id != verification.case_id:
            raise AppError("REPRODUCTION_CALL_NOT_FOUND")
        prior_evaluations = list(verification.evaluations_json or [])
        if any(x.get("verification_call_id") == current_call.id for x in prior_evaluations):
            return verification
        if verification.status in {
            FixVerificationStatus.FIX_VERIFIED.value,
            FixVerificationStatus.FIX_FAILED.value,
            FixVerificationStatus.FIX_REGRESSION.value,
            FixVerificationStatus.FIX_INCONCLUSIVE.value,
        }:
            raise AppError("FIX_VERIFICATION_TERMINAL", details={"verification_id": verification.id, "status": verification.status})
        baseline_findings = set((baseline_call.quick_analysis_json or {}).get("findings") or [])
        current_findings = set((current_call.quick_analysis_json or {}).get("findings") or [])
        allowed_changes = list((fix.metadata_json or {}).get("allowed_environment_changes") or [])
        # Infer the primary intended fix dimension when caller did not declare it explicitly.
        inferred = {
            FixActionType.SOFTWARE_PATCH.value: ["software.version"],
            FixActionType.DEVICE_REPLACE.value: ["device.serial"],
            FixActionType.FXS_PORT_CHANGE.value: ["voice.fxs_port"],
        }.get(fix.action_type, [])
        allowed_changes = list(dict.fromkeys([*allowed_changes, *inferred]))
        env_status, hard_drift = self.compare_environment(
            baseline=baseline_environment,
            verification=verification_environment,
            allowed_change_paths=allowed_changes,
        )
        blocking = list(new_blocking_findings or [])
        baseline_target = verification.target_finding in baseline_findings
        target_still_present = verification.target_finding in current_findings
        business_ok = bool(business_checks) and all(bool(x) for x in business_checks.values())

        # Per-call result is deterministic. Multiple successful verification calls may be
        # required by profile/config before the Case can be RESOLVED.
        if blocking:
            call_result = FixVerificationStatus.FIX_REGRESSION
        elif target_still_present or current_call.verdict == CallVerdict.MATCH.value:
            call_result = FixVerificationStatus.FIX_FAILED
        elif env_status == EnvironmentComparisonStatus.NOT_COMPARABLE or not baseline_target:
            call_result = FixVerificationStatus.FIX_INCONCLUSIVE
        elif current_call.verdict == CallVerdict.NO_MATCH.value and business_ok:
            call_result = FixVerificationStatus.FIX_VERIFIED
        else:
            call_result = FixVerificationStatus.FIX_INCONCLUSIVE

        next_call_count = int(verification.verification_call_count or 0) + 1
        next_success_count = int(verification.successful_call_count or 0) + (1 if call_result == FixVerificationStatus.FIX_VERIFIED else 0)
        if call_result in {FixVerificationStatus.FIX_FAILED, FixVerificationStatus.FIX_REGRESSION}:
            result = call_result
        elif next_success_count >= int(verification.required_calls):
            result = FixVerificationStatus.FIX_VERIFIED
        elif next_call_count >= int(verification.max_calls):
            result = FixVerificationStatus.FIX_INCONCLUSIVE
        else:
            result = FixVerificationStatus.RUNNING

        payload = {
            "schema_version": 1,
            "service_version": self.version,
            "result": result.value,
            "call_result": call_result.value,
            "verification_call_count": next_call_count,
            "successful_call_count": next_success_count,
            "required_calls": verification.required_calls,
            "max_calls": verification.max_calls,
            "target_finding": verification.target_finding,
            "baseline_target_present": baseline_target,
            "verification_target_present": target_still_present,
            "baseline_call_id": baseline_call.id,
            "verification_call_id": current_call.id,
            "environment_status": env_status.value,
            "hard_drift": hard_drift,
            "business_checks": business_checks,
            "new_blocking_findings": blocking,
            "allowed_environment_changes": allowed_changes,
        }
        data = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode()
        sha = hashlib.sha256(data).hexdigest()
        object_key = f"cases/{verification.case_id}/fix-verification/{verification.id}/fix_comparison_{sha[:12]}.json"
        self.storage.put_bytes(object_key, data, "application/json")
        parent_ids: list[str] = []
        for call in (baseline_call, current_call):
            qa = call.quick_analysis_json or {}
            parent_ids.extend(qa.get("input_evidence_ids") or [])
            parent_ids.extend(qa.get("output_evidence_ids") or [])
        parent_ids = [x for x in dict.fromkeys(parent_ids) if db.get(Evidence, x) is not None]
        if not parent_ids:
            raise AppError("FIX_VERIFICATION_EVIDENCE_REQUIRED")
        evidence = create_evidence(
            db,
            case_id=verification.case_id,
            evidence_type="FIX_COMPARISON",
            source="FIX_VERIFICATION_SERVICE",
            filename="fix_comparison.json",
            object_key=object_key,
            size_bytes=len(data),
            sha256=sha,
            kind=EvidenceKind.DERIVED,
            scope=EvidenceScope.CASE,
            level=EvidenceLevel.L1,
            completeness=EvidenceCompleteness.COMPLETE if result in {FixVerificationStatus.FIX_VERIFIED, FixVerificationStatus.FIX_FAILED, FixVerificationStatus.FIX_REGRESSION} else EvidenceCompleteness.PARTIAL,
            content_type="application/json",
            producer_type="FIX_VERIFICATION",
            producer_id=verification.id,
            producer_version=self.version,
            session_id=current_session.id,
            call_id=current_call.id,
            metadata=payload,
            parent_evidence_ids=parent_ids,
            actor=actor,
        )
        verification.verification_session_id = current_session.id
        verification.verification_call_id = current_call.id
        verification.environment_status = env_status.value
        verification.business_checks_json = business_checks
        verification.verification_call_count = next_call_count
        verification.successful_call_count = next_success_count
        evaluation_record = {
            "verification_call_id": current_call.id,
            "verification_session_id": current_session.id,
            "call_result": call_result.value,
            "overall_status": result.value,
            "evidence_id": evidence.id,
            "environment_status": env_status.value,
        }
        verification.evaluations_json = [*prior_evaluations, evaluation_record]
        verification.comparison_json = {"latest": payload, "evaluations": verification.evaluations_json}
        verification.evidence_id = evidence.id
        verification.status = result.value
        db.flush()

        question = db.scalar(
            select(DiagnosticQuestion).where(
                DiagnosticQuestion.case_id == verification.case_id,
                DiagnosticQuestion.question_key == "FIX_VERIFICATION",
                DiagnosticQuestion.state.in_([DiagnosticQuestionState.OPEN.value, DiagnosticQuestionState.IN_PROGRESS.value]),
            ).order_by(DiagnosticQuestion.created_at.desc())
        )
        if question:
            if result == FixVerificationStatus.FIX_VERIFIED:
                DiagnosticQuestionGraph().answer(
                    db,
                    question=question,
                    answer={"result": result.value, "findings": [], "fix_verification_id": verification.id},
                    evidence_refs=[{"evidence_id": evidence.id, "level": "L1"}],
                    actor=actor,
                )
            else:
                question.state = DiagnosticQuestionState.IN_PROGRESS.value
                question.answer_json = {
                    "result": result.value,
                    "fix_verification_id": verification.id,
                    "verification_call_count": verification.verification_call_count,
                    "successful_call_count": verification.successful_call_count,
                }

        case = db.get(Case, verification.case_id)
        if result == FixVerificationStatus.FIX_VERIFIED and case:
            CaseTransitionService.transition(
                db,
                case,
                CaseEvent.FIX_VERIFIED,
                reason="fix verification passed",
                actor=actor,
                context={"fix_verification_evidence_id": evidence.id, "fix_verification_id": verification.id},
            )
        elif result in {FixVerificationStatus.FIX_FAILED, FixVerificationStatus.FIX_INCONCLUSIVE} and case:
            CaseTransitionService.transition(
                db,
                case,
                CaseEvent.FIX_REEVALUATION_REQUIRED,
                reason=result.value,
                actor=actor,
                context={"fix_verification_id": verification.id, "evidence_id": evidence.id},
            )
        # FIX_REGRESSION intentionally keeps Case in RESOLVING for explicit triage of the new blocker.
        emit_event(
            db,
            event_type=EventType.FIX_VERIFICATION_UPDATED,
            case_id=verification.case_id,
            entity_type="fix_verification",
            entity_id=verification.id,
            payload={"status": verification.status, "evidence_id": evidence.id, "environment_status": env_status.value, "verification_call_count": verification.verification_call_count, "successful_call_count": verification.successful_call_count},
        )
        return verification
