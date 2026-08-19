from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_permissions
from app.auth.providers import AuthIdentity
from app.contracts.enums import PermissionName
from app.db.ai_intelligence_models import AIDiagnosticCycle
from app.db.models import Case
from app.diagnosis.ai_cycle import AIDiagnosticCycleError, AIDiagnosticCycleService

router = APIRouter(tags=["ai-diagnostic-loop"])
_reader = require_permissions(PermissionName.CASE_READ, PermissionName.DIAGNOSIS_READ)
_runner = require_permissions(PermissionName.CASE_READ, PermissionName.DIAGNOSIS_RUN)


def _serialize(row: AIDiagnosticCycle, *, replay: bool = False) -> dict:
    return {
        "id": row.id,
        "case_id": row.case_id,
        "cycle_no": row.cycle_no,
        "runtime_stage": row.runtime_stage,
        "snapshot_fingerprint": row.snapshot_fingerprint,
        "evidence_fingerprint": row.evidence_fingerprint,
        "proposal_id": row.proposal_id,
        "status": row.status,
        "known": row.known_json or [],
        "unknown": row.unknown_json or [],
        "excluded": row.excluded_json or [],
        "hypotheses": row.hypotheses_json or [],
        "critic": row.critic_json or {},
        "next_action": row.next_action_json or {},
        "selection": row.selection_json or {},
        "continue_recommendation": row.continue_recommendation,
        "stop_reason": row.stop_reason,
        "no_progress_count": row.no_progress_count,
        # Never mask an invariant violation in the observability API. Normal V1
        # SHADOW/SUGGEST rows are required to persist all three values as false;
        # metrics/release gates can then detect any non-zero violation directly.
        "formal_result_changed": bool(row.formal_result_changed),
        "dispatch_attempted": bool(row.dispatch_attempted),
        "dispatch_allowed": bool(row.dispatch_allowed),
        "suggestion_state": row.suggestion_state,
        "accepted_by": row.accepted_by,
        "accepted_at": row.accepted_at.isoformat() if row.accepted_at else None,
        "execution_ref_type": row.execution_ref_type,
        "execution_ref_id": row.execution_ref_id,
        "suggestion_error_code": row.suggestion_error_code,
        "idempotent_replay": replay,
        "error_code": row.error_code,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "root_cause_authority": "DETERMINISTIC_OR_HUMAN_CONFIRMED_ONLY",
        "execution_authority": "DETERMINISTIC_RBAC_POLICY_ORCHESTRATOR",
    }


def _counts(db: Session, column) -> dict[str, int]:
    rows = db.execute(select(column, func.count()).group_by(column)).all()
    return {str(key or "NONE"): int(count) for key, count in rows}


@router.get("/ai/diagnostic-loop/metrics")
def ai_diagnostic_loop_metrics(
    db: Session = Depends(get_db),
    identity: AuthIdentity = Depends(_reader),
):
    total = int(db.scalar(select(func.count()).select_from(AIDiagnosticCycle)) or 0)
    accepted = int(db.scalar(
        select(func.count()).select_from(AIDiagnosticCycle).where(AIDiagnosticCycle.accepted_by.is_not(None))
    ) or 0)
    return {
        "schema_version": "ai-diagnostic-loop-metrics-v1",
        "total_cycles": total,
        "by_stage": _counts(db, AIDiagnosticCycle.runtime_stage),
        "by_status": _counts(db, AIDiagnosticCycle.status),
        "by_continue_recommendation": _counts(db, AIDiagnosticCycle.continue_recommendation),
        "by_stop_reason": _counts(db, AIDiagnosticCycle.stop_reason),
        "by_suggestion_state": _counts(db, AIDiagnosticCycle.suggestion_state),
        "accepted_suggestions": accepted,
        "ai_formal_result_changes": int(db.scalar(
            select(func.count()).select_from(AIDiagnosticCycle).where(AIDiagnosticCycle.formal_result_changed.is_(True))
        ) or 0),
        "ai_dispatch_attempts": int(db.scalar(
            select(func.count()).select_from(AIDiagnosticCycle).where(AIDiagnosticCycle.dispatch_attempted.is_(True))
        ) or 0),
        "ai_dispatch_allowed_rows": int(db.scalar(
            select(func.count()).select_from(AIDiagnosticCycle).where(AIDiagnosticCycle.dispatch_allowed.is_(True))
        ) or 0),
        "actor_id": identity.actor_id,
    }


@router.get("/cases/{case_id}/ai/cycles")
def list_ai_cycles(
    case_id: str,
    db: Session = Depends(get_db),
    identity: AuthIdentity = Depends(_reader),
):
    if db.get(Case, case_id) is None:
        raise HTTPException(404, "CASE_NOT_FOUND")
    rows = list(db.scalars(
        select(AIDiagnosticCycle)
        .where(AIDiagnosticCycle.case_id == case_id)
        .order_by(AIDiagnosticCycle.cycle_no.desc())
        .limit(100)
    ))
    return {
        "schema_version": "ai-diagnostic-cycle-list-v1",
        "case_id": case_id,
        "items": [_serialize(row) for row in rows],
        "count": len(rows),
        "actor_id": identity.actor_id,
    }


@router.post("/cases/{case_id}/ai/cycles/next")
def run_next_ai_cycle(
    case_id: str,
    db: Session = Depends(get_db),
    identity: AuthIdentity = Depends(_runner),
):
    if db.get(Case, case_id) is None:
        raise HTTPException(404, "CASE_NOT_FOUND")
    try:
        with db.begin_nested():
            execution = AIDiagnosticCycleService().run_next(
                db,
                case_id=case_id,
                actor=identity.actor_id,
            )
    except AIDiagnosticCycleError as exc:
        code = str(exc)
        if code == "AI_DIAGNOSTIC_LOOP_DISABLED":
            raise HTTPException(503, code) from exc
        if code in {
            "AI_DIAGNOSTIC_LOOP_STAGE_OFF",
            "AI2_CONTROLLED_PLANNER_NOT_ENABLED_BY_V1_GATE",
            "AI2_MAX_CYCLES_REACHED",
        }:
            raise HTTPException(409, code) from exc
        raise HTTPException(422, code) from exc
    db.commit()
    return {
        "schema_version": "ai-diagnostic-cycle-response-v1",
        **_serialize(execution.row, replay=execution.idempotent_replay),
    }
