from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.contracts.enums import (
    DiagnosticQuestionState, EnvironmentComparisonStatus, EventType, ExperimentRunStatus,
    ExperimentState, ExperimentVariant, ReproductionCallStatus, ReproductionState,
)
from app.core.errors import AppError
from app.db.models import (
    CausalAssessment, DiagnosticExperiment, DiagnosticQuestion, ExperimentEnvironmentSnapshot,
    ExperimentRun, Hypothesis, ReproductionCall, ReproductionSession,
)
from app.experiments.causal import CausalConfirmationEngine
from app.experiments.environment import EnvironmentComparator, EnvironmentSnapshotBuilder
from app.experiments.profile import ExperimentProfileDefinition, ExperimentProfileRegistry
from app.reproduction.orchestrator import ReproductionOrchestrator
from app.reproduction.question_graph import DiagnosticQuestionGraph
from app.services.audit import audit
from app.services.events import emit_event


def _utcnow():
    return datetime.now(timezone.utc)


class DiagnosticExperimentOrchestrator:
    """Deterministic A/B experiment coordinator on top of ReproductionSession.

    Physical actions remain external. This coordinator only advances after a pre-approved
    ExperimentProfile says what single variable may change. It never executes an EC-02 or
    L3/L4 DUT command.
    """

    def __init__(
        self,
        *,
        registry: ExperimentProfileRegistry | None = None,
        reproduction: ReproductionOrchestrator | None = None,
        questions: DiagnosticQuestionGraph | None = None,
    ):
        self.registry = registry or ExperimentProfileRegistry()
        self.reproduction = reproduction or ReproductionOrchestrator()
        self.questions = questions or DiagnosticQuestionGraph()
        self.snapshots = EnvironmentSnapshotBuilder()
        self.comparator = EnvironmentComparator()
        self.causal = CausalConfirmationEngine()

    def _profile(self, experiment: DiagnosticExperiment) -> ExperimentProfileDefinition:
        # Effective snapshot is frozen at Experiment creation.
        return ExperimentProfileDefinition.model_validate(experiment.effective_profile_snapshot)

    def create_experiment(
        self,
        db: Session,
        *,
        case_id: str,
        profile_id: str,
        hypothesis_id: str | None = None,
        question_id: str | None = None,
        actor: str | None = None,
    ) -> DiagnosticExperiment:
        loaded = self.registry.get(profile_id)
        profile = loaded.definition
        hypothesis = db.get(Hypothesis, hypothesis_id) if hypothesis_id else None
        if hypothesis_id and (not hypothesis or hypothesis.case_id != case_id):
            raise AppError("HYPOTHESIS_NOT_FOUND")
        if hypothesis and profile.hypothesis_codes and hypothesis.code not in profile.hypothesis_codes:
            raise AppError("EXPERIMENT_PROFILE_NOT_APPLICABLE", details={"profile_id": profile_id, "hypothesis_code": hypothesis.code})
        question = db.get(DiagnosticQuestion, question_id) if question_id else None
        if question_id and (not question or question.case_id != case_id):
            raise AppError("DIAGNOSTIC_QUESTION_NOT_FOUND")
        if question:
            allowed = self.questions.candidate_experiments(question.question_key)
            if allowed and profile_id not in allowed:
                raise AppError("EXPERIMENT_PROFILE_NOT_APPLICABLE", details={"profile_id": profile_id, "question_key": question.question_key})
        row = DiagnosticExperiment(
            case_id=case_id,
            hypothesis_id=hypothesis_id,
            question_id=question_id,
            profile_key=profile.id,
            profile_version=profile.version,
            profile_checksum=loaded.checksum,
            effective_profile_snapshot=profile.canonical(),
            state=ExperimentState.CREATED.value,
            confirmation_policy=profile.confirmation_policy.value,
            independent_variable=profile.independent_variable,
            target_finding=profile.target_finding,
            reproduction_profile_id=profile.reproduction_profile_id,
            created_by=actor,
        )
        db.add(row)
        db.flush()
        audit(
            db,
            case_id=case_id,
            actor=actor,
            event_type=EventType.EXPERIMENT_CREATED.value,
            action="DIAGNOSTIC_EXPERIMENT_CREATE",
            target_type="diagnostic_experiment",
            target_id=row.id,
            detail={"profile_id": profile.id, "version": profile.version, "checksum": loaded.checksum, "hypothesis_id": hypothesis_id, "question_id": question_id},
        )
        emit_event(
            db,
            event_type=EventType.EXPERIMENT_CREATED,
            case_id=case_id,
            entity_type="diagnostic_experiment",
            entity_id=row.id,
            payload={"profile_id": row.profile_key, "state": row.state, "confirmation_policy": row.confirmation_policy},
        )
        return row

    def _next_variant(self, db: Session, experiment: DiagnosticExperiment) -> ExperimentVariant | None:
        profile = self._profile(experiment)
        runs = list(db.scalars(select(ExperimentRun).where(ExperimentRun.experiment_id == experiment.id).order_by(ExperimentRun.run_no)))
        for variant in profile.sequence:
            valid = any(x.variant == variant.value and x.status == ExperimentRunStatus.COMPLETED.value for x in runs)
            if not valid:
                return variant
        return None

    def plan_next_run(self, db: Session, *, experiment: DiagnosticExperiment, actor: str | None = None) -> ExperimentRun | None:
        active=db.scalar(select(ExperimentRun).where(
            ExperimentRun.experiment_id==experiment.id,
            ExperimentRun.status.in_([ExperimentRunStatus.WAITING_EXTERNAL_ACTION.value,ExperimentRunStatus.READY.value,ExperimentRunStatus.REPRODUCING.value]),
        ).order_by(ExperimentRun.run_no.desc()))
        if active:
            return active
        variant = self._next_variant(db, experiment)
        if variant is None:
            # Preserve the strongest causal terminal state; COMPLETED is not allowed to erase
            # ROOT_CAUSE_CONFIRMED semantics from the experiment record.
            experiment.state = (
                ExperimentState.ROOT_CAUSE_CONFIRMED.value
                if experiment.causal_state == "ROOT_CAUSE_CONFIRMED"
                else ExperimentState.INCONCLUSIVE.value
            )
            experiment.terminal_reason = experiment.terminal_reason or "SEQUENCE_COMPLETED"
            db.flush()
            return None
        profile = self._profile(experiment)
        run_no = (db.scalar(select(func.max(ExperimentRun.run_no)).where(ExperimentRun.experiment_id == experiment.id)) or 0) + 1
        needs_external = bool(profile.external_action_required and variant != ExperimentVariant.A1)
        run = ExperimentRun(
            experiment_id=experiment.id,
            case_id=experiment.case_id,
            run_no=run_no,
            variant=variant.value,
            status=ExperimentRunStatus.WAITING_EXTERNAL_ACTION.value if needs_external else ExperimentRunStatus.READY.value,
            external_action_required=needs_external,
        )
        db.add(run)
        experiment.current_round = run_no
        experiment.state = ExperimentState.WAITING_EXTERNAL_ACTION.value if needs_external else ExperimentState.IN_PROGRESS.value
        db.flush()
        emit_event(
            db,
            event_type=EventType.EXPERIMENT_RUN_CHANGED,
            case_id=experiment.case_id,
            entity_type="experiment_run",
            entity_id=run.id,
            payload={"experiment_id": experiment.id, "run_no": run.run_no, "variant": run.variant, "status": run.status, "external_action_required": needs_external},
        )
        return run

    def complete_external_action(self, db: Session, *, run: ExperimentRun, actor: str | None = None) -> ExperimentRun:
        if not run.external_action_required:
            return run
        if run.status == ExperimentRunStatus.READY.value and run.external_action_completed_at is not None:
            return run
        if run.status != ExperimentRunStatus.WAITING_EXTERNAL_ACTION.value:
            raise AppError("EXPERIMENT_RUN_TRANSITION_NOT_ALLOWED", details={"run_id": run.id, "state": run.status})
        run.external_action_completed_at = _utcnow()
        run.status = ExperimentRunStatus.READY.value
        experiment = db.get(DiagnosticExperiment, run.experiment_id)
        if experiment:
            experiment.state = ExperimentState.IN_PROGRESS.value
        audit(db,case_id=run.case_id,actor=actor,event_type=EventType.EXPERIMENT_RUN_CHANGED.value,action="EXPERIMENT_EXTERNAL_ACTION_COMPLETED",target_type="experiment_run",target_id=run.id,detail={"variant":run.variant})
        db.flush()
        return run

    def start_reproduction(
        self,
        db: Session,
        *,
        run: ExperimentRun,
        external_state: dict[str, Any] | None = None,
        call_context: dict[str, Any] | None = None,
        environment_overrides: dict[str, Any] | None = None,
        actor: str | None = None,
    ) -> ReproductionSession:
        if run.reproduction_session_id and run.status == ExperimentRunStatus.REPRODUCING.value:
            existing=db.get(ReproductionSession,run.reproduction_session_id)
            if existing:
                return existing
        if run.status != ExperimentRunStatus.READY.value:
            raise AppError("EXPERIMENT_RUN_TRANSITION_NOT_ALLOWED", details={"run_id": run.id, "state": run.status, "operation": "start_reproduction"})
        experiment = db.get(DiagnosticExperiment, run.experiment_id)
        if not experiment:
            raise AppError("EXPERIMENT_NOT_FOUND")
        session = self.reproduction.create_session(
            db,
            case_id=experiment.case_id,
            profile_id=experiment.reproduction_profile_id,
            actor=actor,
        )
        run.reproduction_session_id = session.id
        run.status = ExperimentRunStatus.REPRODUCING.value
        run.started_at = _utcnow()
        self.reproduction.start(db, session=session, actor=actor)
        # PRE is captured after ARM but before the experimental call. No DUT command is issued
        # here; the builder only freezes structured runtime/external state already available.
        existing_pre = db.scalar(select(ExperimentEnvironmentSnapshot).where(
            ExperimentEnvironmentSnapshot.run_id == run.id,
            ExperimentEnvironmentSnapshot.phase == "PRE",
        ).order_by(ExperimentEnvironmentSnapshot.created_at.desc()))
        if not existing_pre:
            self.snapshots.build(
                db,
                experiment=experiment,
                run=run,
                external_state=external_state,
                call_context=call_context,
                phase="PRE",
                overrides=environment_overrides,
            )
        db.flush()
        return session

    def attach_result(
        self,
        db: Session,
        *,
        run: ExperimentRun,
        session_id: str,
        call_id: str,
        external_state: dict[str, Any],
        call_context: dict[str, Any],
        environment_overrides: dict[str, Any] | None = None,
        hard_contradictions: list[str] | None = None,
        actor: str | None = None,
    ) -> CausalAssessment:
        experiment = db.get(DiagnosticExperiment, run.experiment_id)
        if not experiment:
            raise AppError("EXPERIMENT_NOT_FOUND")
        if run.status in {ExperimentRunStatus.COMPLETED.value, ExperimentRunStatus.INVALID.value} and run.reproduction_session_id==session_id and run.reproduction_call_id==call_id:
            existing=db.scalar(select(CausalAssessment).where(CausalAssessment.experiment_id==experiment.id).order_by(CausalAssessment.created_at.desc()))
            if existing:
                return existing
        session = db.get(ReproductionSession, session_id)
        call = db.get(ReproductionCall, call_id)
        if not session or session.case_id != experiment.case_id:
            raise AppError("REPRODUCTION_NOT_FOUND")
        if not call or call.session_id != session.id or call.status != ReproductionCallStatus.ANALYZED.value:
            raise AppError("REPRODUCTION_CALL_NOT_FOUND")
        if run.reproduction_session_id and run.reproduction_session_id != session.id:
            raise AppError("EXPERIMENT_REPRODUCTION_NOT_TERMINAL", details={"reason": "RUN_SESSION_MISMATCH", "expected_session_id": run.reproduction_session_id, "actual_session_id": session.id})
        # A manipulated B run often produces NO_MATCH by design, so the underlying symptom
        # ReproductionProfile may remain WATCHING because its own target evidence is absent.
        # Once the experiment has one analyzed Call, safely finalize that bounded run rather
        # than waiting for an unrelated profile timeout. Cleanup remains mandatory.
        if ReproductionState(session.state) in {ReproductionState.WATCHING, ReproductionState.ACTIVITY_DETECTED}:
            session.terminal_reason = "EXPERIMENT_CALL_COMPLETE"
            self.reproduction.cleanup(db, session=session, actor=actor)
        if ReproductionState(session.state) not in {ReproductionState.COMPLETED, ReproductionState.PARTIAL_SUCCESS, ReproductionState.ANALYZING}:
            raise AppError("EXPERIMENT_REPRODUCTION_NOT_TERMINAL", details={"session_state": session.state})
        profile = self._profile(experiment)
        run.reproduction_session_id = session.id
        run.reproduction_call_id = call.id
        run.target_verdict = call.verdict
        findings = set((call.quick_analysis_json or {}).get("findings") or [])
        run.target_finding_present = profile.target_finding in findings
        run.metrics_json = dict((call.quick_analysis_json or {}).get("metrics") or {})
        run.status = ExperimentRunStatus.COMPLETED.value
        run.finished_at = _utcnow()
        snapshot = self.snapshots.build(
            db,
            experiment=experiment,
            run=run,
            external_state=external_state,
            call_context=call_context,
            phase="POST",
            overrides=environment_overrides,
        )

        # Compare every manipulated/revert run to A1. This makes accidental multi-variable drift blocking.
        if run.variant != ExperimentVariant.A1.value:
            baseline_run = db.scalar(select(ExperimentRun).where(
                ExperimentRun.experiment_id == experiment.id,
                ExperimentRun.variant == ExperimentVariant.A1.value,
                ExperimentRun.status == ExperimentRunStatus.COMPLETED.value,
            ).order_by(ExperimentRun.run_no.desc()))
            if not baseline_run:
                raise AppError("EXPERIMENT_BASELINE_REQUIRED")
            baseline_snapshot = db.scalar(select(ExperimentEnvironmentSnapshot).where(
                ExperimentEnvironmentSnapshot.run_id == baseline_run.id,
                ExperimentEnvironmentSnapshot.phase == "POST",
            ).order_by(ExperimentEnvironmentSnapshot.created_at.desc()))
            if not baseline_snapshot:
                raise AppError("EXPERIMENT_ENVIRONMENT_SNAPSHOT_REQUIRED")
            comparison = self.comparator.compare_and_persist(
                db,
                experiment=experiment,
                profile=profile,
                baseline_run=baseline_run,
                variant_run=run,
                baseline_snapshot=baseline_snapshot,
                variant_snapshot=snapshot,
                revert=(run.variant == ExperimentVariant.A2.value),
            )
            if comparison.status == EnvironmentComparisonStatus.NOT_COMPARABLE.value:
                run.status = ExperimentRunStatus.INVALID.value

        assessment = self.causal.evaluate_and_persist(
            db,
            experiment=experiment,
            profile=profile,
            hard_contradictions=hard_contradictions,
            actor=actor,
        )
        if assessment.state == "ROOT_CAUSE_CONFIRMED" and experiment.question_id:
            question = db.get(DiagnosticQuestion, experiment.question_id)
            if question and question.state != DiagnosticQuestionState.ANSWERED.value:
                self.questions.answer(
                    db,
                    question=question,
                    answer={"result": "ROOT_CAUSE_CONFIRMED", "experiment_id": experiment.id, "assessment_id": assessment.id, "findings": [profile.target_finding]},
                    evidence_refs=[{"ref_type": "CAUSAL_ASSESSMENT", "ref_id": assessment.id, "level": "L1"}],
                    actor=actor,
                )
        emit_event(
            db,
            event_type=EventType.EXPERIMENT_RUN_CHANGED,
            case_id=experiment.case_id,
            entity_type="experiment_run",
            entity_id=run.id,
            payload={"status": run.status, "variant": run.variant, "target_verdict": run.target_verdict, "target_finding_present": run.target_finding_present, "causal_state": assessment.state},
        )
        db.flush()
        return assessment
