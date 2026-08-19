from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.ai_intelligence_models import AIDiagnosticCycle
from app.experiments.orchestrator import DiagnosticExperimentOrchestrator
from app.reproduction.orchestrator import ReproductionOrchestrator
from app.reproduction.question_graph import DiagnosticQuestionGraph, DiagnosticQuestionRegistry
from app.services.audit import audit


class AISuggestionBridgeError(RuntimeError):
    pass


@dataclass(frozen=True)
class AISuggestionExecution:
    cycle: AIDiagnosticCycle
    kind: str
    registered_id: str
    execution_ref_type: str | None
    execution_ref_id: str | None
    user_message: str
    enqueue_after_commit: bool = False
    idempotent_replay: bool = False


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AISuggestionBridge:
    """Explicit-user-confirmation bridge for an AI2 SUGGEST recommendation.

    The AI cycle itself has no dispatch authority. This bridge is called only after
    Feishu Identity/RBAC authorization. It re-loads the persisted cycle, rejects a
    stale/non-SUGGEST/non-registered recommendation, revalidates the identifier in
    the deterministic registry/orchestrator, and only then creates the deterministic
    workflow object. Any asynchronous worker dispatch is deliberately deferred until
    the caller has committed the database transaction.
    """

    def _load_current_cycle(self, db: Session, *, case_id: str, cycle_id: str) -> AIDiagnosticCycle:
        # Serialize acceptance for the requested suggestion. Without a row lock,
        # two simultaneous Feishu card clicks could both observe PROPOSED and each
        # create a deterministic workflow before either transaction commits.
        row = db.scalar(
            select(AIDiagnosticCycle)
            .where(AIDiagnosticCycle.id == cycle_id)
            .with_for_update()
        )
        if not row or row.case_id != case_id:
            raise AISuggestionBridgeError("AI2_SUGGESTION_NOT_FOUND")
        if row.runtime_stage != "SUGGEST":
            raise AISuggestionBridgeError("AI2_SUGGEST_STAGE_REQUIRED")
        if row.status != "COMPLETED":
            raise AISuggestionBridgeError("AI2_SUGGESTION_NOT_ACTIONABLE")
        current = db.scalar(
            select(AIDiagnosticCycle)
            .where(AIDiagnosticCycle.case_id == case_id, AIDiagnosticCycle.runtime_stage == "SUGGEST")
            .order_by(AIDiagnosticCycle.cycle_no.desc(), AIDiagnosticCycle.created_at.desc())
            .limit(1)
            .with_for_update()
        )
        if current is None or current.id != row.id:
            raise AISuggestionBridgeError("AI2_SUGGESTION_STALE")
        return row

    @staticmethod
    def _selection(row: AIDiagnosticCycle) -> tuple[str, str, str]:
        action = dict(row.next_action_json or {})
        kind = str(action.get("type") or "")
        registered_id = str(action.get("registered_id") or "")
        reason = str(action.get("reason") or "AI2 registered recommendation")
        if kind not in {"QUESTION", "REPRODUCTION_PROFILE", "EXPERIMENT_PROFILE", "USER_EVIDENCE_REQUEST"}:
            raise AISuggestionBridgeError("AI2_SUGGESTION_KIND_NOT_ALLOWED")
        if not registered_id:
            raise AISuggestionBridgeError("AI2_SUGGESTION_REGISTERED_ID_MISSING")
        if action.get("raw_command_allowed") is True:
            raise AISuggestionBridgeError("AI2_RAW_COMMAND_FORBIDDEN")
        return kind, registered_id, reason

    def accept(
        self,
        db: Session,
        *,
        case_id: str,
        cycle_id: str,
        actor: str,
        explicit_user_confirmation: bool,
    ) -> AISuggestionExecution:
        if not explicit_user_confirmation:
            raise AISuggestionBridgeError("AI2_EXPLICIT_USER_CONFIRMATION_REQUIRED")
        row = self._load_current_cycle(db, case_id=case_id, cycle_id=cycle_id)
        kind, registered_id, reason = self._selection(row)

        if row.suggestion_state == "DISPATCHED" and row.execution_ref_id:
            return AISuggestionExecution(
                cycle=row,
                kind=kind,
                registered_id=registered_id,
                execution_ref_type=row.execution_ref_type,
                execution_ref_id=row.execution_ref_id,
                user_message="该 AI2 建议已采纳并进入确定性工作流，本次为幂等重复回调。",
                enqueue_after_commit=False,
                idempotent_replay=True,
            )
        if row.suggestion_state != "PROPOSED":
            raise AISuggestionBridgeError("AI2_SUGGESTION_STATE_NOT_ACTIONABLE")

        row.suggestion_state = "ACCEPTED"
        row.accepted_by = actor
        row.accepted_at = row.accepted_at or _utcnow()
        row.suggestion_error_code = None
        db.flush()

        try:
            execution_ref_type: str | None = None
            execution_ref_id: str | None = None
            enqueue_after_commit = False
            message = "AI2 建议已采纳。"

            if kind == "QUESTION":
                template = DiagnosticQuestionRegistry().get(registered_id)
                question = DiagnosticQuestionGraph().ensure_question(
                    db,
                    case_id=case_id,
                    question_key=registered_id,
                    selected_reason=f"ai2_suggest_user_confirmed:{row.id}",
                )
                execution_ref_type = "diagnostic_question"
                execution_ref_id = question.id
                message = f"已采纳建议：{template.title}。请按该问题补充对应现象/证据。"

            elif kind == "REPRODUCTION_PROFILE":
                session = ReproductionOrchestrator().create_session(
                    db,
                    case_id=case_id,
                    profile_id=registered_id,
                    actor=actor,
                )
                execution_ref_type = "reproduction_session"
                execution_ref_id = session.id
                enqueue_after_commit = True
                message = "已按注册复现 Profile 创建任务；提交成功后将进入确定性 Reproduction Orchestrator。"

            elif kind == "EXPERIMENT_PROFILE":
                orchestrator = DiagnosticExperimentOrchestrator()
                experiment = orchestrator.create_experiment(
                    db,
                    case_id=case_id,
                    profile_id=registered_id,
                    actor=actor,
                )
                orchestrator.plan_next_run(db, experiment=experiment, actor=actor)
                execution_ref_type = "diagnostic_experiment"
                execution_ref_id = experiment.id
                message = "已按注册 Experiment Profile 创建 A/B 实验；后续步骤继续由确定性 Experiment Orchestrator 管理。"

            else:
                execution_ref_type = "user_evidence_request"
                execution_ref_id = row.id
                message = "已采纳建议：请在当前 Case 群继续上传/补充建议所需的现场证据。"

            row.suggestion_state = "DISPATCHED"
            row.execution_ref_type = execution_ref_type
            row.execution_ref_id = execution_ref_id
            row.dispatch_attempted = False
            row.dispatch_allowed = False
            row.formal_result_changed = False
            db.flush()
            audit(
                db,
                case_id=case_id,
                actor=actor,
                event_type="AI_DIAGNOSTIC_SUGGESTION_ACCEPTED",
                action="AI2_SUGGESTION_USER_CONFIRMED",
                target_type="ai_diagnostic_cycle",
                target_id=row.id,
                detail={
                    "schema_version": "ai2-suggestion-acceptance-v1",
                    "kind": kind,
                    "registered_id": registered_id,
                    "reason": reason,
                    "execution_ref_type": execution_ref_type,
                    "execution_ref_id": execution_ref_id,
                    "explicit_user_confirmation": True,
                    "ai_dispatch_authority": False,
                    "async_dispatch_deferred_until_commit": enqueue_after_commit,
                    "raw_command_allowed": False,
                    "formal_result_changed": False,
                },
            )
            return AISuggestionExecution(
                cycle=row,
                kind=kind,
                registered_id=registered_id,
                execution_ref_type=execution_ref_type,
                execution_ref_id=execution_ref_id,
                user_message=message,
                enqueue_after_commit=enqueue_after_commit,
            )
        except Exception as exc:
            row.suggestion_state = "FAILED"
            row.suggestion_error_code = f"{type(exc).__name__}:{exc}"[:128]
            db.flush()
            raise
