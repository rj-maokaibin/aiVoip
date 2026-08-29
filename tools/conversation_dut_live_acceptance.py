#!/usr/bin/env python3
"""Strict read-only acceptance for one real Feishu Conversation -> DUT diagnostic flow.

This gate does not create Cases, ConversationTurns, ReproductionSessions, calls, Evidence,
or replies. It only observes the normal Production product flow and verifies that one
explicitly tagged Feishu acceptance incident stayed inside one Case and one real-DUT
ReproductionSession through capture, deterministic analysis/diagnosis, cleanup and a
post-diagnosis Feishu reply.

The real call must be produced by a real phone/user. No FXS/SIP/RTP lifecycle event is
simulated by this tool.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from datetime import timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.db.conversation_models import Conversation, ConversationTurn, FeishuReplyDeliveryTrace
from app.db.models import (
    Case,
    FeishuCaseBinding,
    IdempotencyRecord,
    ReproductionCall,
    ReproductionEventRecord,
    ReproductionSession,
)
from app.db.session import SessionLocal
from tools.m7_acceptance_strict_audit import collect_strict, is_strict_real_session

CONTRACT = "conversation-dut-live-acceptance-v1"
TAG_RE = re.compile(r"^CONV-DUT-E2E-[A-Z0-9_-]{8,64}$")
TRANSIENT_PHASES = {
    "WAITING_CONVERSATION_TURN",
    "WAITING_REPRODUCTION",
    "WAITING_ARM",
    "WAITING_REAL_CALL",
    "WAITING_CLEANUP",
    "WAITING_ANALYSIS",
    "WAITING_REPLY",
}
TERMINAL_REPRO_STATES = {"COMPLETED", "PARTIAL_SUCCESS", "CANCELLED", "FAILED"}
VERIFIED_CLEANUP = {"CLEANUP_VERIFIED", "CLEANUP_VERIFIED_EXTERNAL_WAIT"}

# M7 criteria reused here. AI SHADOW and Golden promotion are deliberately excluded:
# this Gate validates the product diagnostic chain, not model promotion/maturity.
REQUIRED_M7_KEYS = (
    "case_exists",
    "dut_bound",
    "voice_context_ready",
    "pcap_present",
    "pcm_rx_present",
    "pcm_tx_present",
    "debug_present",
    "packet_analyzer_success",
    "media_analyzer_success",
    "deterministic_diagnosis_ready",
    "reproduction_armed",
    "call_detected",
    "cleanup_verified",
    "no_active_lock",
    "report_generated",
)


def _sha256(value: str | None) -> str | None:
    text = str(value or "")
    return hashlib.sha256(text.encode("utf-8")).hexdigest() if text else None


def _aware(value: Any) -> Any:
    if value is not None and getattr(value, "tzinfo", None) is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def validate_acceptance_tag(value: str) -> str:
    tag = str(value or "").strip()
    if not TAG_RE.fullmatch(tag):
        raise RuntimeError("CONVERSATION_DUT_ACCEPTANCE_TAG_INVALID")
    return tag


def _resolve_case(db, value: str) -> Case | None:
    ref = str(value or "").strip()
    row = db.get(Case, ref)
    if row is not None:
        return row
    return db.scalar(select(Case).where(Case.case_no == ref).limit(1))


def _criterion_map(m7: dict[str, Any]) -> dict[str, bool]:
    return {
        str(row.get("key")): str(row.get("status")) == "PASS"
        for row in (m7.get("criteria") or [])
        if row.get("key")
    }


def _target_session_from_m7(db, m7: dict[str, Any]) -> ReproductionSession | None:
    sid = str(((m7.get("observed") or {}).get("target_session") or {}).get("id") or "")
    return db.get(ReproductionSession, sid) if sid else None


def _conversation_rows(db, *, case_id: str, binding: FeishuCaseBinding) -> tuple[list[Conversation], list[ConversationTurn]]:
    candidates = list(
        db.scalars(
            select(Conversation)
            .where(
                Conversation.channel == "FEISHU",
                Conversation.chat_id == binding.receive_id,
                Conversation.status == "ACTIVE",
            )
            .order_by(Conversation.updated_at.desc())
        )
    )
    if binding.source_tenant_key:
        candidates = [row for row in candidates if row.tenant_key == binding.source_tenant_key]
    conversations = [row for row in candidates if row.active_case_id == case_id]
    conversation_ids = [row.id for row in conversations]
    turns: list[ConversationTurn] = []
    if conversation_ids:
        turns = list(
            db.scalars(
                select(ConversationTurn)
                .where(
                    ConversationTurn.conversation_id.in_(conversation_ids),
                    ConversationTurn.direction == "USER",
                )
                .order_by(ConversationTurn.created_at.asc())
            )
        )
    return conversations, turns


def _diagnosis_after_target(db, *, case_id: str, target: ReproductionSession | None):
    if target is None:
        return None
    anchor = _aware(target.started_at or target.created_at)
    rows = list(
        db.scalars(
            select(__import__("app.db.models", fromlist=["DiagnosisRun"]).DiagnosisRun)
            .where(__import__("app.db.models", fromlist=["DiagnosisRun"]).DiagnosisRun.case_id == case_id)
            .order_by(__import__("app.db.models", fromlist=["DiagnosisRun"]).DiagnosisRun.created_at.desc())
        )
    )
    for row in rows:
        if anchor is None or _aware(row.created_at) >= anchor:
            return row
    return None


def _completion_feedback(db, *, case_id: str, diagnosis_id: str | None):
    if not diagnosis_id:
        return None
    key = f"{case_id}:COMPLETED:{diagnosis_id}"
    return db.scalar(
        select(IdempotencyRecord)
        .where(
            IdempotencyRecord.scope == "FEISHU_CASE_FEEDBACK",
            IdempotencyRecord.idempotency_key == key,
        )
        .order_by(IdempotencyRecord.created_at.desc())
        .limit(1)
    )


def _post_diagnosis_reply(
    db,
    *,
    case_id: str,
    source_message_id: str,
    diagnosis_finished_at: Any,
) -> FeishuReplyDeliveryTrace | None:
    query = select(FeishuReplyDeliveryTrace).where(
        FeishuReplyDeliveryTrace.case_id == case_id,
        FeishuReplyDeliveryTrace.source_message_id == source_message_id,
        FeishuReplyDeliveryTrace.stage == "SENT",
    )
    if diagnosis_finished_at is not None:
        query = query.where(FeishuReplyDeliveryTrace.created_at >= diagnosis_finished_at)
    return db.scalar(query.order_by(FeishuReplyDeliveryTrace.created_at.desc()).limit(1))


def _flow_event_types(db, *, session_id: str | None) -> set[str]:
    if not session_id:
        return set()
    return {
        str(value)
        for value in db.scalars(
            select(ReproductionEventRecord.event_type).where(
                ReproductionEventRecord.session_id == session_id
            )
        )
    }


def _analyzed_calls(db, *, session_id: str | None) -> list[ReproductionCall]:
    if not session_id:
        return []
    return list(
        db.scalars(
            select(ReproductionCall)
            .where(
                ReproductionCall.session_id == session_id,
                ReproductionCall.status == "ANALYZED",
            )
            .order_by(ReproductionCall.created_at.asc())
        )
    )


def _phase(*, checks: dict[str, bool], target: ReproductionSession | None, analyzed_calls: list[Any]) -> str:
    hard_ingress = (
        checks.get("feishu_binding")
        and checks.get("dedicated_acceptance_tag")
        and checks.get("feishu_source_message")
    )
    if not hard_ingress:
        return "BLOCKED"
    if not checks.get("conversation_bound") or not checks.get("conversation_user_turn"):
        return "WAITING_CONVERSATION_TURN"
    if target is None:
        return "WAITING_REPRODUCTION"
    if not checks.get("real_dut_session") or not checks.get("same_case_session"):
        return "BLOCKED"
    if not checks.get("fxs_monitor_ready"):
        if str(target.state or "").upper() in TERMINAL_REPRO_STATES:
            return "BLOCKED"
        return "WAITING_ARM"
    if not analyzed_calls:
        if str(target.state or "").upper() in TERMINAL_REPRO_STATES:
            return "BLOCKED"
        return "WAITING_REAL_CALL"
    if not checks.get("cleanup_verified"):
        return "WAITING_CLEANUP"
    if not checks.get("capture_analysis_complete") or not checks.get("diagnosis_completed"):
        return "WAITING_ANALYSIS"
    if not checks.get("completion_feedback_queued") or not checks.get("post_diagnosis_reply_sent"):
        return "WAITING_REPLY"
    return "PASS" if all(checks.values()) else "BLOCKED"


def collect_once(db, *, case_ref: str, acceptance_tag: str) -> dict[str, Any]:
    tag = validate_acceptance_tag(acceptance_tag)
    case = _resolve_case(db, case_ref)
    if case is None:
        return {
            "contract": CONTRACT,
            "status": "BLOCKED",
            "phase": "BLOCKED",
            "blocker_codes": ["CASE_NOT_FOUND"],
            "requested_case": case_ref,
        }

    binding = db.scalar(
        select(FeishuCaseBinding)
        .where(FeishuCaseBinding.case_id == case.id, FeishuCaseBinding.status == "ACTIVE")
        .order_by(FeishuCaseBinding.created_at.desc())
        .limit(1)
    )
    source_message_id = str(getattr(binding, "source_message_id", "") or "")
    source_text = str(getattr(binding, "source_normalized_text", "") or "")
    conversations: list[Conversation] = []
    turns: list[ConversationTurn] = []
    if binding is not None:
        conversations, turns = _conversation_rows(db, case_id=case.id, binding=binding)

    m7 = collect_strict(db, case)
    m7_map = _criterion_map(m7)
    target = _target_session_from_m7(db, m7)
    session_id = str(getattr(target, "id", "") or "") or None
    events = _flow_event_types(db, session_id=session_id)
    analyzed_calls = _analyzed_calls(db, session_id=session_id)
    diagnosis = _diagnosis_after_target(db, case_id=case.id, target=target)
    diagnosis_finished = _aware(getattr(diagnosis, "finished_at", None)) if diagnosis else None
    completion = _completion_feedback(
        db, case_id=case.id, diagnosis_id=str(getattr(diagnosis, "id", "") or "") or None
    )
    reply = None
    if source_message_id and diagnosis is not None:
        reply = _post_diagnosis_reply(
            db,
            case_id=case.id,
            source_message_id=source_message_id,
            diagnosis_finished_at=diagnosis_finished,
        )

    required_m7 = {key: bool(m7_map.get(key)) for key in REQUIRED_M7_KEYS}
    capture_analysis_complete = all(required_m7.values())
    material_turns = [
        row for row in turns
        if row.case_id == case.id and bool(row.material_diagnostic_context)
    ]
    source_turns = [row for row in turns if row.source_message_id == source_message_id]
    feedback_status = str(((completion.response_json or {}) if completion else {}).get("status") or "")

    checks = {
        "feishu_binding": binding is not None and bool(binding.receive_id),
        "dedicated_acceptance_tag": bool(binding is not None and tag in source_text),
        "feishu_source_message": source_message_id.startswith("om_") and len(source_message_id) >= 12,
        "conversation_bound": bool(conversations),
        "conversation_user_turn": bool(turns),
        "conversation_material_context": bool(material_turns or source_turns),
        "real_dut_session": bool(target is not None and is_strict_real_session(target)),
        "same_case_session": bool(target is not None and target.case_id == case.id),
        "fxs_monitor_ready": "FXS_MONITOR_READY" in events,
        "real_call_analyzed": bool(analyzed_calls),
        "capture_analysis_complete": capture_analysis_complete,
        "cleanup_verified": bool(
            target is not None and str(target.cleanup_status or "").upper() in VERIFIED_CLEANUP
        ),
        "diagnosis_completed": bool(
            diagnosis is not None
            and str(diagnosis.status or "").upper() == "DIAGNOSED"
            and isinstance(diagnosis.decision_json, dict)
            and bool(diagnosis.decision_json)
        ),
        "completion_feedback_queued": feedback_status == "QUEUED",
        "post_diagnosis_reply_sent": bool(reply is not None and reply.stage == "SENT"),
    }
    phase = _phase(checks=checks, target=target, analyzed_calls=analyzed_calls)
    blockers = [key for key, passed in checks.items() if not passed]

    return {
        "contract": CONTRACT,
        "status": "PASS" if phase == "PASS" else ("WAITING" if phase in TRANSIENT_PHASES else "BLOCKED"),
        "phase": phase,
        "case": {
            "id": case.id,
            "case_no": case.case_no,
            "status": case.status,
        },
        "ingress": {
            "binding_id": getattr(binding, "id", None),
            "source_message_sha256": _sha256(source_message_id),
            "sender_sha256": _sha256(getattr(binding, "source_sender_open_id", None)) if binding else None,
            "tenant_sha256": _sha256(getattr(binding, "source_tenant_key", None)) if binding else None,
            "chat_sha256": _sha256(getattr(binding, "receive_id", None)) if binding else None,
            "acceptance_tag_sha256": _sha256(tag),
        },
        "conversation": {
            "ids": [row.id for row in conversations],
            "user_turn_ids": [row.id for row in turns],
            "material_turn_ids": [row.id for row in material_turns],
            "source_turn_ids": [row.id for row in source_turns],
        },
        "reproduction": None if target is None else {
            "session_id": target.id,
            "state": target.state,
            "platform_profile_id": target.platform_profile_id,
            "cleanup_status": target.cleanup_status,
            "capture_completeness": target.capture_completeness,
            "fxs_monitor_ready": "FXS_MONITOR_READY" in events,
            "call_ids": [row.id for row in analyzed_calls],
        },
        "m7_required": required_m7,
        "diagnosis": None if diagnosis is None else {
            "run_id": diagnosis.id,
            "status": diagnosis.status,
            "reasoner_name": diagnosis.reasoner_name,
            "finished_at": str(diagnosis.finished_at or ""),
        },
        "reply": None if reply is None else {
            "trace_id": reply.id,
            "stage": reply.stage,
            "attempt_count": int(reply.attempt_count or 0),
            "sent_message_sha256": _sha256(reply.sent_message_id),
            "completion_feedback_record_id": getattr(completion, "id", None),
        },
        "checks": checks,
        "blocker_codes": blockers,
        "safety": {
            "auditor_read_only": True,
            "synthetic_feishu_turn_created": False,
            "synthetic_call_event_created": False,
            "dut_action_executed_by_gate": False,
            "pbx_action_executed_by_gate": False,
            "golden_promotion_required": False,
            "ai_promotion_required": False,
        },
    }


def wait_for_result(*, case_ref: str, acceptance_tag: str, wait_seconds: int, poll_seconds: int) -> dict[str, Any]:
    deadline = time.monotonic() + max(0, int(wait_seconds))
    last: dict[str, Any] | None = None
    while True:
        with SessionLocal() as db:
            last = collect_once(db, case_ref=case_ref, acceptance_tag=acceptance_tag)
        if last.get("status") == "PASS":
            return last
        if last.get("status") == "BLOCKED":
            return last
        if time.monotonic() >= deadline:
            return {
                **last,
                "status": "BLOCKED",
                "error": "ACCEPTANCE_TIMEOUT",
                "last_phase": last.get("phase"),
                "blocker_codes": ["ACCEPTANCE_TIMEOUT", *(last.get("blocker_codes") or [])],
            }
        time.sleep(max(1, int(poll_seconds)))


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only real Conversation -> DUT live acceptance")
    parser.add_argument("--case", required=True, help="Dedicated acceptance Case id or case_no")
    parser.add_argument("--acceptance-tag", required=True)
    parser.add_argument("--wait-seconds", type=int, default=0)
    parser.add_argument("--poll-seconds", type=int, default=10)
    parser.add_argument("--out", type=Path, default=Path("validation/conversation_dut_live_acceptance_v1.json"))
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()

    try:
        payload = wait_for_result(
            case_ref=args.case,
            acceptance_tag=args.acceptance_tag,
            wait_seconds=args.wait_seconds,
            poll_seconds=args.poll_seconds,
        )
    except Exception as exc:
        payload = {
            "contract": CONTRACT,
            "status": "BLOCKED",
            "phase": "BLOCKED",
            "error": type(exc).__name__,
            "error_message": str(exc)[:300],
        }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps({
        "contract": payload.get("contract"),
        "status": payload.get("status"),
        "phase": payload.get("phase"),
        "case_no": ((payload.get("case") or {}).get("case_no")),
        "session_id": ((payload.get("reproduction") or {}).get("session_id")),
        "blocker_codes": payload.get("blocker_codes", []),
        "out": str(args.out),
    }, ensure_ascii=False, indent=2))
    if args.strict and payload.get("status") != "PASS":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
