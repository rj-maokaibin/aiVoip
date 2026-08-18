from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.contracts.enums import CaseStatus
from app.core.config import settings
from app.db.ai_intelligence_models import AIDiagnosticCycle
from app.diagnosis.ai_proposal import run_ai_shadow
from app.diagnosis.ai_runtime import AIPromotionStage, AIRuntimePolicy
from app.diagnosis.ai_workbench import contradiction_critic, controlled_planning
from app.diagnosis.case_intelligence_snapshot import CaseIntelligenceSnapshotBuilder
from app.diagnosis.controlled_ai_selection import (
    ControlledAISelectionError,
    resolve_registered_selection,
)
from app.diagnosis.gateway import ReasoningGatewayClient
from app.services.audit import audit


_TERMINAL_CASE_STATES = {
    CaseStatus.ROOT_CAUSE_CONFIRMED.value,
    CaseStatus.RESOLVED.value,
    CaseStatus.CLOSED.value,
}


class AIDiagnosticCycleError(RuntimeError):
    pass


@dataclass(frozen=True)
class CycleExecution:
    row: AIDiagnosticCycle
    idempotent_replay: bool = False


def _deterministic_baseline(snapshot: dict) -> dict:
    diagnoses = snapshot.get("diagnoses") or []
    diagnosis = diagnoses[0] if diagnoses else {}
    summary = dict(diagnosis.get("summary") or {})
    decision = dict(diagnosis.get("decision") or {})
    hypotheses = list(snapshot.get("hypotheses") or decision.get("hypotheses") or [])
    known = list(decision.get("known") or summary.get("known") or [])
    unknown = list(decision.get("unknown") or summary.get("unknown") or [])
    excluded = list(decision.get("excluded") or summary.get("excluded") or [])
    return {
        "schema_version": "deterministic-diagnosis-baseline-v1",
        "diagnosis_run_id": diagnosis.get("id"),
        "hypotheses": hypotheses,
        "known": known,
        "unknown": unknown,
        "excluded": excluded,
        "summary": summary,
        "decision": decision,
        "formal_authority": "DETERMINISTIC_ONLY",
    }


def _evidence_sufficient(snapshot: dict) -> bool:
    for reproduction in snapshot.get("reproductions") or []:
        if str(reproduction.get("evidence_sufficiency") or "").upper() in {
            "SUFFICIENT",
            "COMPLETE",
            "PASS",
        }:
            return True
    return False


def _next_action_from_selection(selection, proposal: dict, planning: dict | None = None) -> dict:
    action = proposal.get("recommended_action") or {}
    expected = []
    distinguishes = []
    if planning:
        question = planning.get("question_recommendation") or {}
        expected = list(question.get("possible_outcomes") or [])
        distinguishes = list(question.get("distinguishes") or [])
    return {
        "type": selection.kind,
        "registered_id": selection.registered_id,
        "reason": selection.reason,
        "expected_outcomes": expected,
        "distinguishes": distinguishes,
        "source_action_type": action.get("action_type"),
        "dispatch_allowed": False,
        "raw_command_allowed": False,
    }


class AIDiagnosticCycleService:
    """AI2 cognitive-loop orchestrator for SHADOW and SUGGEST.

    This service deliberately stops before Policy/Orchestrator dispatch. SHADOW
    records hypothesis/critic only. SUGGEST may validate and expose a registered
    recommendation, but it never dispatches it. CONTROLLED_PLANNER remains owned by
    the existing promotion gate + deterministic Policy/Orchestrator path and is not
    enabled by this V1 software gate.
    """

    def __init__(
        self,
        *,
        snapshot_builder: CaseIntelligenceSnapshotBuilder | None = None,
        gateway: ReasoningGatewayClient | None = None,
        runtime: AIRuntimePolicy | None = None,
    ):
        self.snapshot_builder = snapshot_builder or CaseIntelligenceSnapshotBuilder()
        self.gateway = gateway or ReasoningGatewayClient()
        self.runtime = runtime

    def run_next(self, db: Session, *, case_id: str, actor: str = "ai2-cycle") -> CycleExecution:
        if not settings.ai_diagnostic_loop_enabled:
            raise AIDiagnosticCycleError("AI_DIAGNOSTIC_LOOP_DISABLED")

        runtime = self.runtime or AIRuntimePolicy.from_settings(settings)
        if runtime.stage is AIPromotionStage.OFF:
            raise AIDiagnosticCycleError("AI_DIAGNOSTIC_LOOP_STAGE_OFF")
        if runtime.stage is AIPromotionStage.CONTROLLED_PLANNER:
            raise AIDiagnosticCycleError("AI2_CONTROLLED_PLANNER_NOT_ENABLED_BY_V1_GATE")

        snapshot = self.snapshot_builder.build_for_reasoning(db, case_id)
        snapshot_fingerprint = str(snapshot.get("snapshot_fingerprint") or snapshot.get("fingerprint") or "")
        if not snapshot_fingerprint:
            raise AIDiagnosticCycleError("AI2_SNAPSHOT_FINGERPRINT_MISSING")
        evidence_fingerprint = str(snapshot.get("source_evidence_fingerprint") or snapshot_fingerprint)

        existing = db.scalar(
            select(AIDiagnosticCycle).where(
                AIDiagnosticCycle.case_id == case_id,
                AIDiagnosticCycle.snapshot_fingerprint == snapshot_fingerprint,
                AIDiagnosticCycle.runtime_stage == runtime.stage.value,
            ).limit(1)
        )
        if existing is not None:
            return CycleExecution(existing, idempotent_replay=True)

        previous = list(db.scalars(
            select(AIDiagnosticCycle)
            .where(AIDiagnosticCycle.case_id == case_id)
            .order_by(AIDiagnosticCycle.cycle_no.asc())
        ))
        cycle_no = len(previous) + 1
        if cycle_no > int(settings.diagnosis_max_cycles):
            raise AIDiagnosticCycleError("AI2_MAX_CYCLES_REACHED")

        no_progress_count = 0
        if previous and previous[-1].evidence_fingerprint == evidence_fingerprint:
            no_progress_count = int(previous[-1].no_progress_count or 0) + 1

        baseline = _deterministic_baseline(snapshot)
        status = "COMPLETED"
        continue_recommendation = "CONTINUE"
        stop_reason: str | None = None
        proposal_record = None
        proposal: dict = {}
        critic: dict = {
            "status": "NOT_RUN",
            "hard_contradictions": [],
            "unsupported_claims": [],
            "alternative_explanations": [],
            "missing_discriminating_evidence": [],
        }
        next_action: dict = {}
        selection_json: dict = {}
        error_code: str | None = None

        case_status = str((snapshot.get("case") or {}).get("status") or "")
        if case_status in _TERMINAL_CASE_STATES:
            status = "STOPPED"
            continue_recommendation = "STOP"
            stop_reason = "ROOT_CAUSE_OR_CASE_TERMINAL"
        elif _evidence_sufficient(snapshot):
            status = "STOPPED"
            continue_recommendation = "STOP"
            stop_reason = "EVIDENCE_SUFFICIENT"
        elif no_progress_count >= int(settings.diagnosis_no_progress_limit):
            status = "STOPPED"
            continue_recommendation = "STOP"
            stop_reason = "NO_PROGRESS_LIMIT"
        else:
            proposal_record = run_ai_shadow(
                db,
                case_id=case_id,
                diagnosis_run_id=baseline.get("diagnosis_run_id"),
                snapshot=snapshot,
                deterministic_baseline=baseline,
                gateway=self.gateway,
            )
            # Reuse the existing immutable proposal contract; the AI2 cycle records
            # the effective promotion stage separately. For SUGGEST, marking this
            # sidecar record as SUGGEST is audit metadata only and never increases
            # execution or formal diagnosis authority.
            proposal_record.mode = runtime.stage.value
            proposal = dict(proposal_record.validated_output_json or {})
            if proposal_record.status != "ACCEPTED" or not proposal:
                status = "DEGRADED"
                continue_recommendation = "REQUIRE_HUMAN"
                stop_reason = "AI_PROPOSAL_UNAVAILABLE"
                error_code = proposal_record.gateway_error or (
                    (proposal_record.validation_errors or [{}])[0].get("code")
                    if proposal_record.validation_errors
                    else "AI_PROPOSAL_NOT_ACCEPTED"
                )
            else:
                critic = contradiction_critic(baseline, proposal)
                if critic.get("hard_contradictions"):
                    status = "REQUIRE_HUMAN"
                    continue_recommendation = "REQUIRE_HUMAN"
                    stop_reason = "HARD_CONTRADICTION"

                if runtime.stage is AIPromotionStage.SUGGEST and continue_recommendation == "CONTINUE":
                    try:
                        selection = resolve_registered_selection(
                            proposal,
                            runtime=runtime,
                            source_proposal_id=proposal_record.id,
                            require_dispatch_authority=False,
                        )
                        planning = None
                        if selection is None:
                            planning = controlled_planning(snapshot, baseline)
                            question = planning.get("question_recommendation") or {}
                            question_key = str(question.get("question_key") or "")
                            fallback_proposal = {
                                "recommended_action": {
                                    "action_type": "RECOMMEND_QUESTION",
                                    "question_key": question_key,
                                    "reason": str(question.get("reason") or "registered discriminator"),
                                }
                            }
                            selection = resolve_registered_selection(
                                fallback_proposal,
                                runtime=runtime,
                                source_proposal_id=proposal_record.id,
                                require_dispatch_authority=False,
                            )
                            proposal_for_action = fallback_proposal
                        else:
                            proposal_for_action = proposal
                        if selection is None:
                            raise ControlledAISelectionError("AI2_NO_REGISTERED_DISCRIMINATOR")
                        selection_json = selection.to_dict()
                        # SUGGEST never dispatches, even if future runtime plumbing changes.
                        selection_json["dispatch_allowed"] = False
                        next_action = _next_action_from_selection(
                            selection,
                            proposal_for_action,
                            planning=planning,
                        )
                    except Exception as exc:
                        status = "REQUIRE_HUMAN"
                        continue_recommendation = "REQUIRE_HUMAN"
                        stop_reason = "NO_USEFUL_REGISTERED_DISCRIMINATOR"
                        error_code = f"{type(exc).__name__}:{exc}"[:128]

        if cycle_no >= int(settings.diagnosis_max_cycles) and continue_recommendation == "CONTINUE":
            status = "STOPPED"
            continue_recommendation = "STOP"
            stop_reason = "MAX_CYCLES_REACHED"

        row = AIDiagnosticCycle(
            case_id=case_id,
            cycle_no=cycle_no,
            runtime_stage=runtime.stage.value,
            snapshot_fingerprint=snapshot_fingerprint,
            evidence_fingerprint=evidence_fingerprint,
            proposal_id=proposal_record.id if proposal_record is not None else None,
            status=status,
            known_json=list(proposal.get("known") or baseline.get("known") or []),
            unknown_json=list(proposal.get("unknown") or baseline.get("unknown") or []),
            excluded_json=list(proposal.get("excluded") or baseline.get("excluded") or []),
            hypotheses_json=list(proposal.get("hypotheses") or []),
            critic_json=critic,
            next_action_json=next_action,
            selection_json=selection_json,
            continue_recommendation=continue_recommendation,
            stop_reason=stop_reason,
            no_progress_count=no_progress_count,
            formal_result_changed=False,
            dispatch_attempted=False,
            dispatch_allowed=False,
            error_code=error_code,
        )
        db.add(row)
        db.flush()
        audit(
            db,
            case_id=case_id,
            actor=actor,
            event_type="AI_DIAGNOSTIC_CYCLE_EVALUATED",
            target_type="ai_diagnostic_cycle",
            target_id=row.id,
            detail={
                "schema_version": "ai-diagnostic-cycle-audit-v1",
                "cycle_no": cycle_no,
                "runtime_stage": runtime.stage.value,
                "status": status,
                "continue_recommendation": continue_recommendation,
                "stop_reason": stop_reason,
                "no_progress_count": no_progress_count,
                "proposal_id": row.proposal_id,
                "registered_id": next_action.get("registered_id"),
                "formal_result_changed": False,
                "dispatch_attempted": False,
                "dispatch_allowed": False,
                "raw_command_allowed": False,
                "root_cause_confirmed_by_ai": False,
            },
        )
        return CycleExecution(row)
