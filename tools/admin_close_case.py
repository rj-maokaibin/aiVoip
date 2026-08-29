#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

from sqlalchemy import select

from app.contracts.enums import CaseStatus, ReproductionState
from app.db.models import Case, CaseStateHistory, FeishuCaseBinding, ReproductionSession
from app.db.session import SessionLocal
from app.integrations.feishu.case_resolver import (
    active_case_for_chat,
    close_binding_lifecycle,
    lifecycle_columns_available,
)
from app.services.audit import audit
from app.services.case_transitions import ADMIN_CLOSE_EVENT, CaseTransitionService

CASE_RE = re.compile(r"^VOIP-\d{8}-[A-Z0-9]{6}$")
TERMINAL_REPRODUCTION_STATES = {
    ReproductionState.COMPLETED.value,
    ReproductionState.PARTIAL_SUCCESS.value,
    ReproductionState.CANCELLED.value,
    ReproductionState.FAILED.value,
}


def _sha(value: str | None) -> str | None:
    raw = str(value or "").strip()
    return hashlib.sha256(raw.encode("utf-8")).hexdigest() if raw else None


def _active_reproductions(db, case_id: str) -> list[ReproductionSession]:
    return list(db.scalars(
        select(ReproductionSession)
        .where(
            ReproductionSession.case_id == case_id,
            ReproductionSession.state.not_in(sorted(TERMINAL_REPRODUCTION_STATES)),
        )
        .order_by(ReproductionSession.created_at.desc())
    ))


def _binding_for_case(db, case_id: str) -> FeishuCaseBinding | None:
    return db.scalar(
        select(FeishuCaseBinding)
        .where(FeishuCaseBinding.case_id == case_id)
        .order_by(FeishuCaseBinding.created_at.desc())
        .limit(1)
    )


def close_case(*, case_no: str, actor: str, reason: str, apply: bool) -> dict:
    if not CASE_RE.fullmatch(case_no):
        raise SystemExit("ADMIN_CLOSE_CASE_NO_INVALID")
    if not actor.startswith("github-admin:"):
        raise SystemExit("ADMIN_CLOSE_ACTOR_INVALID")
    reason = reason.strip()
    if not reason:
        raise SystemExit("ADMIN_CLOSE_REASON_REQUIRED")

    with SessionLocal() as db:
        case = db.scalar(select(Case).where(Case.case_no == case_no).limit(1))
        if case is None:
            raise SystemExit("ADMIN_CLOSE_CASE_NOT_FOUND")

        active = _active_reproductions(db, case.id)
        binding = _binding_for_case(db, case.id)
        lifecycle_available = lifecycle_columns_available(db)
        preflight = {
            "schema_version": "admin-close-case-v1",
            "case_no": case.case_no,
            "case_id_sha256": _sha(case.id),
            "status_before": case.status,
            "active_reproduction_count": len(active),
            "active_reproduction_state_set": sorted({row.state for row in active}),
            "feishu_binding_present": binding is not None,
            "feishu_binding_lifecycle_available": lifecycle_available,
            "feishu_chat_sha256": _sha(binding.receive_id) if binding else None,
            "feishu_tenant_sha256": _sha(binding.source_tenant_key) if binding else None,
            "apply_requested": bool(apply),
        }
        if active:
            return {**preflight, "status": "BLOCKED_ACTIVE_REPRODUCTION"}
        if binding is not None and binding.receive_id_type == "chat_id" and not lifecycle_available:
            return {**preflight, "status": "BLOCKED_BINDING_LIFECYCLE_UNAVAILABLE"}
        if not apply:
            return {**preflight, "status": "PREFLIGHT_PASS"}

        CaseTransitionService.administrative_close(
            db,
            case,
            actor=actor,
            reason=reason,
            context={
                "reason_code": "ADMIN_CLOSED_STALE_CASE_FOR_CONVERSATION_ACCEPTANCE",
                "source": "admin-close-case-live-v1",
            },
        )
        if binding is not None and binding.receive_id_type == "chat_id":
            close_binding_lifecycle(
                db,
                binding_id=binding.id,
                reason="CASE_ADMIN_CLOSED",
            )
            audit(
                db,
                case_id=case.id,
                actor=actor,
                event_type="FEISHU_CASE_BINDING_ADMIN_CLOSED",
                target_type="feishu_case_binding",
                target_id=binding.id,
                reason=reason,
                detail={"case_no": case.case_no, "administrative": True},
            )
        db.commit()

    with SessionLocal() as verify:
        case = verify.scalar(select(Case).where(Case.case_no == case_no).limit(1))
        if case is None or case.status != CaseStatus.CLOSED.value:
            raise SystemExit("ADMIN_CLOSE_VERIFY_CASE_STATUS_FAILED")
        active = _active_reproductions(verify, case.id)
        if active:
            raise SystemExit("ADMIN_CLOSE_VERIFY_ACTIVE_REPRODUCTION_PRESENT")
        history = verify.scalar(
            select(CaseStateHistory)
            .where(
                CaseStateHistory.case_id == case.id,
                CaseStateHistory.event == ADMIN_CLOSE_EVENT,
                CaseStateHistory.to_status == CaseStatus.CLOSED.value,
            )
            .order_by(CaseStateHistory.created_at.desc())
            .limit(1)
        )
        if history is None and preflight["status_before"] != CaseStatus.CLOSED.value:
            raise SystemExit("ADMIN_CLOSE_VERIFY_HISTORY_FAILED")
        binding = _binding_for_case(verify, case.id)
        active_case_after = None
        if binding is not None and binding.receive_id_type == "chat_id" and binding.receive_id:
            active_case_after, _ = active_case_for_chat(
                verify,
                tenant_key=binding.source_tenant_key,
                chat_id=binding.receive_id,
            )
            if active_case_after is not None and active_case_after.id == case.id:
                raise SystemExit("ADMIN_CLOSE_VERIFY_FEISHU_BINDING_STILL_ACTIVE")
        return {
            **preflight,
            "status": "PASS",
            "status_after": case.status,
            "admin_close_history_present": history is not None,
            "active_reproduction_count_after": 0,
            "feishu_active_case_released": active_case_after is None or active_case_after.id != case.id,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="Audited administrative close for one exact VOIP Case")
    parser.add_argument("--case-no", required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    result = close_case(
        case_no=args.case_no.strip().upper(),
        actor=args.actor.strip(),
        reason=args.reason,
        apply=args.apply,
    )
    payload = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0 if result["status"] in {"PREFLIGHT_PASS", "PASS"} else 3


if __name__ == "__main__":
    raise SystemExit(main())
