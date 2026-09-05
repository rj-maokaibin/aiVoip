from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.contracts.enums import (
    CallVerdict, CaseEvent, CausalConclusionState, ConfirmationPolicy, EnvironmentComparisonStatus,
    EventType, ExperimentState, ExperimentVariant, HypothesisState,
)
from app.db.models import (
    Case, CausalAssessment, DiagnosticExperiment, EnvironmentComparison, ExperimentRun,
    Hypothesis, HypothesisEvidence, HypothesisRevision,
)
from app.experiments.profile import ExperimentProfileDefinition
from app.services.case_transitions import CaseTransitionService
from app.services.events import emit_event


@dataclass(frozen=True)
class CausalDecision:
    state: CausalConclusionState
    supporting_run_ids: tuple[str, ...]
    environment_comparison_ids: tuple[str, ...]
    hard_contradictions: tuple[str, ...]
    rationale: dict

    @property
    def root_cause_confirmed(self) -> bool:
        return self.state == CausalConclusionState.ROOT_CAUSE_CONFIRMED


class CausalConfirmationEngine:
    """Deterministic experiment causal gate.

    Confidence scores are intentionally not used as a confirmation gate. The result is
    determined by experiment pattern + environment comparability + contradiction rules.
    """

    version = "1.1.0"

    @staticmethod
    def _run_by_variant(runs: list[ExperimentRun], variant: ExperimentVariant) -> ExperimentRun | None:
        candidates = [x for x in runs if x.variant == variant.value and x.status == "COMPLETED"]
        return sorted(candidates, key=lambda x: (x.run_no, x.id))[-1] if candidates else None

    @staticmethod
    def _latest_terminal_by_variant(runs: list[ExperimentRun], variant: ExperimentVariant) -> ExperimentRun | None:
        # INVALID is terminal for that attempt but retryable at the Experiment layer.
        candidates = [x for x in runs if x.variant == variant.value and x.status in {"COMPLETED", "INVALID"}]
        return sorted(candidates, key=lambda x: (x.run_no, x.id))[-1] if candidates else None

    @staticmethod
    def _comparison_between(comparisons: list[EnvironmentComparison], a: ExperimentRun, b: ExperimentRun) -> EnvironmentComparison | None:
        return next((x for x in comparisons if x.baseline_run_id == a.id and x.variant_run_id == b.id), None)

    def evaluate(
        self,
        *,
        profile: ExperimentProfileDefinition,
        runs: list[ExperimentRun],
        comparisons: list[EnvironmentComparison],
        hard_contradictions: list[str] | None = None,
        direct_evidence: bool = False,
    ) -> CausalDecision:
        contradictions = tuple(hard_contradictions or [])
        if contradictions:
            return CausalDecision(CausalConclusionState.CONTRADICTED, (), (), contradictions, {"reason": "HARD_CONTRADICTION"})

        a1 = self._run_by_variant(runs, ExperimentVariant.A1)
        b = self._run_by_variant(runs, ExperimentVariant.B)
        a2 = self._run_by_variant(runs, ExperimentVariant.A2)
        repeats = [x for x in runs if x.variant == ExperimentVariant.REPEAT.value and x.status == "COMPLETED"]

        if profile.confirmation_policy == ConfirmationPolicy.DIRECT_EVIDENCE:
            state = CausalConclusionState.ROOT_CAUSE_CONFIRMED if direct_evidence else CausalConclusionState.SUPPORTED
            return CausalDecision(state, tuple(x.id for x in runs if x.status == "COMPLETED"), (), (), {"reason": "DIRECT_EVIDENCE" if direct_evidence else "DIRECT_EVIDENCE_MISSING"})

        # Environment hard drift blocks the *current* variant attempt. A later valid retry of
        # the same B/A2/REPEAT variant supersedes the invalid attempt for causal gating; old
        # comparisons remain immutable audit evidence but must not poison the experiment forever.
        latest_terminal = {
            variant: self._latest_terminal_by_variant(runs, variant)
            for variant in (ExperimentVariant.B, ExperimentVariant.A2, ExperimentVariant.REPEAT)
        }
        active_variant_run_ids = {x.id for x in latest_terminal.values() if x is not None}
        bad = [
            x for x in comparisons
            if x.status == EnvironmentComparisonStatus.NOT_COMPARABLE.value
            and x.variant_run_id in active_variant_run_ids
        ]
        if bad:
            return CausalDecision(
                CausalConclusionState.NOT_COMPARABLE,
                tuple(x.id for x in runs if x.status == "COMPLETED"),
                tuple(x.id for x in bad),
                (),
                {"reason": "HARD_ENVIRONMENT_DRIFT", "comparison_ids": [x.id for x in bad]},
            )

        def target(row: ExperimentRun | None) -> bool:
            return bool(row and row.target_finding_present is True and row.target_verdict == CallVerdict.MATCH.value)

        def control(row: ExperimentRun | None) -> bool:
            return bool(row and row.target_finding_present is False and row.target_verdict == CallVerdict.NO_MATCH.value)

        ab_cmp = self._comparison_between(comparisons, a1, b) if a1 and b else None
        aba_cmp = self._comparison_between(comparisons, a1, a2) if a1 and a2 else None
        comparable_ab = bool(ab_cmp and ab_cmp.status != EnvironmentComparisonStatus.NOT_COMPARABLE.value)
        comparable_aba = bool(aba_cmp and aba_cmp.status != EnvironmentComparisonStatus.NOT_COMPARABLE.value)

        if profile.causal_pattern == "A1_CONTROL_B_TARGET_A2_CONTROL":
            pattern_ab = control(a1) and target(b) and comparable_ab
            pattern_aba = pattern_ab and control(a2) and comparable_aba
            same_direction_ab = bool(a1 and b and ((control(a1) and control(b)) or (target(a1) and target(b))))
        else:
            pattern_ab = target(a1) and control(b) and comparable_ab
            pattern_aba = pattern_ab and target(a2) and comparable_aba
            same_direction_ab = bool(a1 and b and ((target(a1) and target(b)) or (control(a1) and control(b))))

        policy = profile.confirmation_policy
        if policy == ConfirmationPolicy.AB_SUFFICIENT:
            state = CausalConclusionState.ROOT_CAUSE_CONFIRMED if pattern_ab else CausalConclusionState.INCONCLUSIVE
        elif policy == ConfirmationPolicy.ABA_REQUIRED:
            if pattern_aba:
                state = CausalConclusionState.ROOT_CAUSE_CONFIRMED
            elif pattern_ab:
                state = CausalConclusionState.STRONGLY_SUPPORTED
            elif same_direction_ab:
                state = CausalConclusionState.CONTRADICTED
            else:
                state = CausalConclusionState.INCONCLUSIVE
        elif policy == ConfirmationPolicy.ABA_PREFERRED:
            if pattern_aba:
                state = CausalConclusionState.ROOT_CAUSE_CONFIRMED
            elif pattern_ab:
                state = CausalConclusionState.STRONGLY_SUPPORTED
            else:
                state = CausalConclusionState.INCONCLUSIVE
        elif policy == ConfirmationPolicy.REPEAT_MATCH:
            # Stable-state A1 is expected to be control; two independent manipulated runs must reproduce target.
            manipulated = ([b] if b else []) + repeats
            comparable_ids = [x.id for x in comparisons if x.status != EnvironmentComparisonStatus.NOT_COMPARABLE.value]
            enough_matches = len([x for x in manipulated if target(x)]) >= 2
            state = (
                CausalConclusionState.ROOT_CAUSE_CONFIRMED
                if a1 and control(a1) and enough_matches and len(comparable_ids) >= 2
                else CausalConclusionState.STRONGLY_SUPPORTED
                if enough_matches
                else CausalConclusionState.INCONCLUSIVE
            )
        else:
            state = CausalConclusionState.INCONCLUSIVE

        support = tuple(x.id for x in [a1, b, a2, *repeats] if x is not None)
        support_set = set(support)
        comp_ids = tuple(
            x.id for x in comparisons
            if x.baseline_run_id in support_set and x.variant_run_id in support_set
        )
        return CausalDecision(
            state,
            support,
            comp_ids,
            (),
            {
                "policy": policy.value,
                "causal_pattern": profile.causal_pattern,
                "pattern_ab": pattern_ab,
                "pattern_aba": pattern_aba,
                "a1_verdict": a1.target_verdict if a1 else None,
                "b_verdict": b.target_verdict if b else None,
                "a2_verdict": a2.target_verdict if a2 else None,
                "repeat_matches": len([x for x in repeats if target(x)]),
            },
        )

    def evaluate_and_persist(
        self,
        db: Session,
        *,
        experiment: DiagnosticExperiment,
        profile: ExperimentProfileDefinition,
        hard_contradictions: list[str] | None = None,
        direct_evidence: bool = False,
        actor: str | None = None,
    ) -> CausalAssessment:
        runs = list(db.scalars(select(ExperimentRun).where(ExperimentRun.experiment_id == experiment.id).order_by(ExperimentRun.run_no)))
        comparisons = list(db.scalars(select(EnvironmentComparison).where(EnvironmentComparison.experiment_id == experiment.id).order_by(EnvironmentComparison.created_at)))
        decision = self.evaluate(profile=profile, runs=runs, comparisons=comparisons, hard_contradictions=hard_contradictions, direct_evidence=direct_evidence)
        assessment = CausalAssessment(
            experiment_id=experiment.id,
            case_id=experiment.case_id,
            hypothesis_id=experiment.hypothesis_id,
            state=decision.state.value,
            confirmation_policy=profile.confirmation_policy.value,
            supporting_run_ids_json=list(decision.supporting_run_ids),
            environment_comparison_ids_json=list(decision.environment_comparison_ids),
            hard_contradictions_json=list(decision.hard_contradictions),
            rationale_json=decision.rationale,
        )
        db.add(assessment)
        experiment.causal_state = decision.state.value
        experiment.state = {
            CausalConclusionState.ROOT_CAUSE_CONFIRMED: ExperimentState.ROOT_CAUSE_CONFIRMED.value,
            CausalConclusionState.STRONGLY_SUPPORTED: ExperimentState.STRONGLY_SUPPORTED.value,
            CausalConclusionState.NOT_COMPARABLE: ExperimentState.NOT_COMPARABLE.value,
            CausalConclusionState.CONTRADICTED: ExperimentState.CONTRADICTED.value,
            CausalConclusionState.INCONCLUSIVE: ExperimentState.INCONCLUSIVE.value,
        }.get(decision.state, ExperimentState.EVALUATING.value)
        db.flush()
        if experiment.hypothesis_id:
            self._append_hypothesis_revision(db, experiment=experiment, assessment=assessment, decision=decision)
        if decision.root_cause_confirmed:
            case = db.get(Case, experiment.case_id)
            if case:
                CaseTransitionService.transition(
                    db,
                    case,
                    CaseEvent.ROOT_CAUSE_CONFIRMED,
                    reason=f"causal experiment {experiment.profile_key} confirmed root cause",
                    actor=actor,
                    context={"experiment_id": experiment.id, "causal_assessment_id": assessment.id},
                )
            emit_event(
                db,
                event_type=EventType.ROOT_CAUSE_CAUSALLY_CONFIRMED,
                case_id=experiment.case_id,
                entity_type="diagnostic_experiment",
                entity_id=experiment.id,
                payload={"assessment_id": assessment.id, "profile_id": experiment.profile_key, "hypothesis_id": experiment.hypothesis_id},
            )
        emit_event(
            db,
            event_type=EventType.CAUSAL_ASSESSMENT_UPDATED,
            case_id=experiment.case_id,
            entity_type="causal_assessment",
            entity_id=assessment.id,
            payload={"experiment_id": experiment.id, "state": assessment.state, "policy": assessment.confirmation_policy},
        )
        return assessment

    @staticmethod
    def _append_hypothesis_revision(
        db: Session,
        *,
        experiment: DiagnosticExperiment,
        assessment: CausalAssessment,
        decision: CausalDecision,
    ) -> None:
        hypothesis = db.get(Hypothesis, experiment.hypothesis_id)
        if not hypothesis:
            return
        mapping = {
            CausalConclusionState.ROOT_CAUSE_CONFIRMED: HypothesisState.CONFIRMED,
            CausalConclusionState.STRONGLY_SUPPORTED: HypothesisState.STRONGLY_SUPPORTED,
            CausalConclusionState.CONTRADICTED: HypothesisState.CONTRADICTED,
        }
        target = mapping.get(decision.state)
        if not target:
            return
        max_rev = db.scalar(select(func.max(HypothesisRevision.revision_no)).where(HypothesisRevision.hypothesis_id == hypothesis.id)) or 0
        revision = HypothesisRevision(
            hypothesis_id=hypothesis.id,
            revision_no=max_rev + 1,
            supersedes_revision_id=hypothesis.current_revision_id,
            title=hypothesis.title,
            fault_domain=hypothesis.fault_domain,
            status=target.value,
            confidence=hypothesis.confidence,
            rationale=f"Causal experiment {experiment.profile_key}: {decision.state.value}",
            confirmable=1,
            confirm_rule=f"EXPERIMENT:{experiment.profile_key}:{experiment.confirmation_policy}",
        )
        db.add(revision)
        db.flush()
        db.add(HypothesisEvidence(
            hypothesis_id=hypothesis.id,
            hypothesis_revision_id=revision.id,
            ref_type="CAUSAL_ASSESSMENT",
            ref_id=assessment.id,
            evidence_level="L1",
            direction="SUPPORT" if target != HypothesisState.CONTRADICTED else "CONTRADICT",
            weight=1000,
            rationale=f"Deterministic {experiment.confirmation_policy} experiment gate",
            details_json={"experiment_id": experiment.id, "assessment_state": assessment.state},
        ))
        hypothesis.status = target.value
        hypothesis.current_revision_id = revision.id
        hypothesis.confirmable = 1
        hypothesis.confirm_rule = revision.confirm_rule
        hypothesis.rationale = revision.rationale
        db.flush()
