#!/usr/bin/env python3
"""Strict, read-only audit for one real-DUT M7 flow.

The canonical M7 gate is intentionally compatibility-friendly and case-scoped.
This audit is stricter: the latest reproduction in the Case must itself be a
real-DUT session, and evidence/analyzer provenance must remain inside that one
flow. It detects cross-session mosaicking that could make a Case-level 20/20
report look healthy even when individual facts came from different sessions.

It never SSHes to a DUT and never mutates DB/session state.
"""
from __future__ import annotations

import argparse
import json
from datetime import timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.db.golden_models import GoldenCandidateAssessment
from app.db.models import (
    AIProposalRecord,
    AnalyzerRun,
    ArmValidationResult,
    AuditLog,
    CaptureChannelHealth,
    Case,
    CaseDevice,
    CleanupRun,
    DeviceDiagnosticLock,
    DiagnosisReport,
    DiagnosisRun,
    EventOutbox,
    Evidence,
    ReproductionCall,
    ReproductionCaptureSegment,
    ReproductionSession,
    VoiceRuntimeContextSnapshot,
)
from app.db.session import SessionLocal
from tools.m7_acceptance_gate import CRITERIA, _ai_authority_safe, evaluate_signals

SCHEMA_VERSION = "m7-real-dut-strict-audit-v1"
KNOWN_REAL_PLATFORM_IDS = {"ruijie-voip-aim-real", "ruijie-voip-capture-v2"}
KNOWN_REAL_RESOLVERS = {"REAL_VOICE_CONTEXT_V1"}
SUCCESS_RUN_STATUSES = {"SUCCESS", "PARTIAL_SUCCESS", "SUCCEEDED"}
VERIFIED_CLEANUP_STATUSES = {"CLEANUP_VERIFIED", "CLEANUP_VERIFIED_EXTERNAL_WAIT"}
TERMINAL_CALL_STATUSES = {"ENDED", "ANALYZING", "ANALYZED"}
ACTIVE_LOCK_STATUSES = {"ACTIVE", "QUARANTINED"}
TERMINAL_REPRO_STATES = {"COMPLETED", "PARTIAL_SUCCESS", "CANCELLED"}


def _safe_json(value: Any) -> str:
    try:
        return json.dumps(value or {}, ensure_ascii=False, sort_keys=True)
    except Exception:
        return str(value or "")


def _evidence_text(row: Evidence) -> str:
    return " ".join(
        [
            str(row.type or ""),
            str(row.source or ""),
            str(row.filename or ""),
            str(row.content_type or ""),
            _safe_json(row.metadata_json),
        ]
    ).upper()


def _has_token(rows: list[Evidence], *tokens: str) -> bool:
    wanted = tuple(x.upper() for x in tokens)
    return any(any(token in _evidence_text(row) for token in wanted) for row in rows)


def is_strict_real_session(row: Any) -> bool:
    """Recognize only production platform ids or the exact legacy real resolver."""
    profile = str(getattr(row, "platform_profile_id", "") or "").strip().lower()
    if profile in KNOWN_REAL_PLATFORM_IDS:
        return True
    ctx = getattr(row, "voice_runtime_context_json", None) or {}
    resolver = str(ctx.get("resolver_id") or "").strip().upper()
    return profile.startswith("mock") and resolver in KNOWN_REAL_RESOLVERS


def select_target_session(rows: list[Any]) -> Any | None:
    """Return the latest reproduction only when that exact flow is real-DUT."""
    if not rows:
        return None
    latest = max(rows, key=lambda row: getattr(row, "created_at", None))
    return latest if is_strict_real_session(latest) else None


def analyzer_uses_target_evidence(row: Any, target_evidence_ids: set[str]) -> bool:
    inputs = {str(x) for x in (getattr(row, "input_evidence_ids", None) or []) if x}
    return bool(inputs & target_evidence_ids)


def linked_analyzers_with_provenance(
    rows: list[Any], seed_evidence_ids: set[str]
) -> tuple[list[Any], set[str]]:
    """Build a provenance closure from target-session Evidence through analyzers."""
    proven = {str(x) for x in seed_evidence_ids if x}
    pending = [
        row for row in rows
        if str(getattr(row, "status", "") or "").upper() in SUCCESS_RUN_STATUSES
    ]
    linked: list[Any] = []
    changed = True
    while changed:
        changed = False
        for row in list(pending):
            if not analyzer_uses_target_evidence(row, proven):
                continue
            linked.append(row)
            pending.remove(row)
            proven.update(
                str(x) for x in (getattr(row, "output_evidence_ids", None) or []) if x
            )
            changed = True
    return linked, proven


def channel_health_detail(rows: list[Any]) -> dict[str, dict[str, Any]]:
    detail: dict[str, dict[str, Any]] = {}
    for row in rows:
        channel = str(getattr(row, "channel", "") or "").upper()
        if not channel:
            continue
        detail[channel] = {
            "status": str(getattr(row, "status", "") or ""),
            "packet_count": int(getattr(row, "packet_count", 0) or 0),
            "last_observed_at": str(getattr(row, "last_observed_at", None) or ""),
            "health_json": getattr(row, "health_json", None) or {},
        }
    return detail


def _aware(value: Any) -> Any:
    if value is not None and getattr(value, "tzinfo", None) is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _after_anchor(row: Any, anchor: Any) -> bool:
    created = getattr(row, "created_at", None)
    if created is None or anchor is None:
        return True
    return _aware(created) >= _aware(anchor)


def _resolve_case(db, value: str) -> Case | None:
    row = db.get(Case, value)
    if row:
        return row
    return db.scalar(select(Case).where(Case.case_no == value).limit(1))


def _blocked_no_target(case: Case, sessions: list[Any]) -> dict[str, Any]:
    signals = {key: False for _, key, _, _ in CRITERIA}
    latest = max(sessions, key=lambda row: getattr(row, "created_at", None)) if sessions else None
    if not sessions:
        blocker = "REPRODUCTION_SESSION_NOT_FOUND"
    elif any(is_strict_real_session(row) for row in sessions):
        blocker = "LATEST_REPRODUCTION_NOT_REAL_OR_AMBIGUOUS"
    else:
        blocker = "REAL_DUT_SESSION_NOT_FOUND"
    report = evaluate_signals(
        signals,
        observed={
            "case": {"id": case.id, "case_no": case.case_no},
            "latest_session": None if latest is None else {
                "id": latest.id,
                "platform_profile_id": latest.platform_profile_id,
                "state": latest.state,
            },
        },
    )
    report["schema_version"] = SCHEMA_VERSION
    report["strict_blockers"] = [blocker]
    return report


def collect_strict(db, case: Case) -> dict[str, Any]:
    sessions = list(
        db.scalars(
            select(ReproductionSession)
            .where(ReproductionSession.case_id == case.id)
            .order_by(ReproductionSession.created_at.desc())
        )
    )
    target = select_target_session(sessions)
    if target is None:
        return _blocked_no_target(case, sessions)

    sid = target.id
    anchor = target.started_at or target.created_at
    device = db.get(CaseDevice, target.device_id)
    calls = list(db.scalars(select(ReproductionCall).where(ReproductionCall.session_id == sid)))
    call_ids = {row.id for row in calls}
    segments = list(db.scalars(select(ReproductionCaptureSegment).where(ReproductionCaptureSegment.session_id == sid)))
    health = list(db.scalars(select(CaptureChannelHealth).where(CaptureChannelHealth.session_id == sid)))
    arm = list(db.scalars(select(ArmValidationResult).where(ArmValidationResult.session_id == sid)))
    cleanup_runs = list(db.scalars(select(CleanupRun).where(CleanupRun.session_id == sid)))
    voice = list(db.scalars(select(VoiceRuntimeContextSnapshot).where(VoiceRuntimeContextSnapshot.session_id == sid)))

    evidences = list(db.scalars(select(Evidence).where(Evidence.case_id == case.id)))
    target_evidence = [
        row for row in evidences
        if str(row.session_id or "") == str(sid) or (row.call_id and row.call_id in call_ids)
    ]
    target_evidence_ids = {str(row.id) for row in target_evidence}

    analyzers = list(db.scalars(select(AnalyzerRun).where(AnalyzerRun.case_id == case.id)))
    linked_analyzers, proven_evidence_ids = linked_analyzers_with_provenance(
        analyzers, target_evidence_ids
    )

    diagnoses = [
        row for row in db.scalars(
            select(DiagnosisRun)
            .where(DiagnosisRun.case_id == case.id)
            .order_by(DiagnosisRun.created_at.desc())
        )
        if _after_anchor(row, anchor)
        and isinstance(row.decision_json, dict)
        and bool(row.decision_json)
        and str(row.status or "").upper() not in {"FAILED", "UNAVAILABLE"}
    ]
    diagnosis = diagnoses[0] if diagnoses else None

    proposals = [
        row for row in db.scalars(
            select(AIProposalRecord)
            .where(AIProposalRecord.case_id == case.id)
            .order_by(AIProposalRecord.created_at.desc())
        )
        if _after_anchor(row, anchor)
        and (diagnosis is None or row.diagnosis_run_id in {None, diagnosis.id})
    ]
    shadow = [row for row in proposals if row.mode == "SHADOW"]
    accepted = [row for row in shadow if row.status == "ACCEPTED"]

    reports = [
        row for row in db.scalars(select(DiagnosisReport).where(DiagnosisReport.case_id == case.id))
        if _after_anchor(row, anchor)
    ]
    locks = []
    if device is not None:
        locks = list(db.scalars(select(DeviceDiagnosticLock).where(DeviceDiagnosticLock.device_id == device.id)))

    all_audit_rows = list(db.scalars(select(AuditLog).where(AuditLog.case_id == case.id)))
    all_outbox_rows = list(db.scalars(select(EventOutbox).where(EventOutbox.case_id == case.id)))
    case_event_set = {str(row.event_type) for row in all_audit_rows} | {
        str(row.event_type) for row in all_outbox_rows
    }
    flow_event_set = {
        str(row.event_type) for row in all_audit_rows if _after_anchor(row, anchor)
    } | {
        str(row.event_type) for row in all_outbox_rows if _after_anchor(row, anchor)
    }
    golden = db.scalar(select(GoldenCandidateAssessment).where(GoldenCandidateAssessment.case_id == case.id))

    segment_channels = {str(row.channel or "").upper() for row in segments}
    ch = channel_health_detail(health)
    pcap_present = "PCAP" in segment_channels or _has_token(target_evidence, "PCAP", "PCAPNG")
    pcm_rx_present = (
        "PCM_RX" in segment_channels
        or _has_token(target_evidence, "PCM_RX", "PCM RX")
        or ch.get("PCM_RX", {}).get("packet_count", 0) > 0
    )
    pcm_tx_present = (
        "PCM_TX" in segment_channels
        or _has_token(target_evidence, "PCM_TX", "PCM TX")
        or ch.get("PCM_TX", {}).get("packet_count", 0) > 0
    )
    debug_present = (
        bool(segment_channels & {"DEBUG", "LOG"})
        or _has_token(target_evidence, "DEBUG", "AIM.LOG", "AIM_LOG", "SYSLOG")
        or any(
            str(ch.get(name, {}).get("status") or "").upper() == "HEALTHY"
            for name in ("DEBUG", "LOG")
        )
    )
    packet_analyzer_success = any(
        any(token in str(row.analyzer_name or "").upper() for token in ("PACKET", "PCAP", "SIP", "RTP"))
        for row in linked_analyzers
    )
    media_analyzer_success = any(
        any(token in str(row.analyzer_name or "").upper() for token in ("PCM", "MEDIA", "AUDIO", "RTP"))
        for row in linked_analyzers
    )
    voice_context_ready = any(
        bool(row.interface_up and row.voice_interface and row.voice_gateway_ip) for row in voice
    )
    reproduction_armed = any(str(row.status or "").upper() == "PASSED" for row in arm)
    call_detected = any(
        bool(row.ended_at)
        or str(row.status or "").upper() in TERMINAL_CALL_STATUSES
        or bool(row.quick_analysis_json)
        for row in calls
    )
    cleanup_verified = (
        str(target.cleanup_status or "").upper() in VERIFIED_CLEANUP_STATUSES
        and (
            not cleanup_runs
            or any(str(row.status or "").upper() == "VERIFIED" for row in cleanup_runs)
        )
    )
    no_active_lock = not any(
        str(row.status or "").upper() in ACTIVE_LOCK_STATUSES for row in locks
    )
    report_generated = any(
        str(row.status or "").upper() == "GENERATED"
        and bool(row.html_object_key)
        and bool(row.json_object_key)
        for row in reports
    )
    ai_grounded = any(
        isinstance(row.validated_output_json, dict) and not (row.validation_errors or [])
        for row in accepted
    )

    flow_audit_groups = [
        {"EVIDENCE_CREATED", "EVIDENCE_UPLOADED"},
        {"ANALYZER_COMPLETED", "PACKET_ANALYSIS_FINISHED", "MEDIA_ANALYSIS_FINISHED", "PCM_ANALYSIS_FINISHED"},
        {"DIAGNOSIS_STARTED", "DIAGNOSIS_CYCLE", "DIAGNOSIS_UPDATED"},
        {"AI_PROPOSAL_EVALUATED"},
        {"REPRODUCTION_CREATED", "REPRODUCTION_STATE_CHANGED"},
        {"REPRODUCTION_ARM_VALIDATED"},
        {"REPRODUCTION_CALL_CHANGED", "TARGET_CONFIRMED"},
        {"REPRODUCTION_CLEANUP_VALIDATED"},
        {"REPORT_READY", "DIAGNOSIS_REPORT_GENERATED"},
    ]
    audit_complete = (
        "CASE_CREATED" in case_event_set
        and all(bool(flow_event_set & group) for group in flow_audit_groups)
    )

    signals = {
        "case_exists": True,
        "dut_bound": device is not None,
        "voice_context_ready": voice_context_ready,
        "pcap_present": pcap_present,
        "pcm_rx_present": pcm_rx_present,
        "pcm_tx_present": pcm_tx_present,
        "debug_present": debug_present,
        "packet_analyzer_success": packet_analyzer_success,
        "media_analyzer_success": media_analyzer_success,
        "deterministic_diagnosis_ready": diagnosis is not None,
        "ai_shadow_present": bool(shadow),
        "ai_grounded": ai_grounded,
        "ai_authority_safe": _ai_authority_safe(proposals),
        "reproduction_armed": reproduction_armed,
        "call_detected": call_detected,
        "cleanup_verified": cleanup_verified,
        "no_active_lock": no_active_lock,
        "report_generated": report_generated,
        "golden_materialized": golden is not None,
        "audit_complete": audit_complete,
    }

    observed = {
        "case": {"id": case.id, "case_no": case.case_no, "status": case.status},
        "target_session": {
            "id": sid,
            "state": target.state,
            "platform_profile_id": target.platform_profile_id,
            "platform_profile_version": target.platform_profile_version,
            "cleanup_status": target.cleanup_status,
            "capture_completeness": target.capture_completeness,
            "created_at": str(target.created_at),
            "started_at": str(target.started_at),
            "legacy_real_resolver_fallback": str(target.platform_profile_id or "").lower().startswith("mock"),
        },
        "device": None if device is None else {
            "id": device.id,
            "sn": device.sn,
            "platform_id": device.platform_id,
            "ssh_port": device.ssh_port,
        },
        "channel_health": ch,
        "capture_segments": [
            {"id": row.id, "channel": row.channel, "status": getattr(row, "status", None)}
            for row in segments
        ],
        "target_evidence": {
            "count": len(target_evidence),
            "ids": sorted(target_evidence_ids),
            "types": sorted({str(row.type) for row in target_evidence}),
            "provenance_closure_ids": sorted(proven_evidence_ids),
        },
        "linked_analyzers": [
            {
                "id": row.id,
                "name": row.analyzer_name,
                "status": row.status,
                "input_evidence_ids": row.input_evidence_ids or [],
                "output_evidence_ids": row.output_evidence_ids or [],
            }
            for row in linked_analyzers
        ],
        "diagnosis": None if diagnosis is None else {
            "id": diagnosis.id,
            "status": diagnosis.status,
            "reasoner": diagnosis.reasoner_name,
        },
        "ai_shadow": [
            {
                "id": row.id,
                "status": row.status,
                "model": row.model_name,
                "validation_errors": row.validation_errors or [],
            }
            for row in shadow[:10]
        ],
        "calls": [
            {"id": row.id, "status": row.status, "verdict": row.verdict, "role": row.role}
            for row in calls
        ],
        "cleanup_runs": [{"id": row.id, "status": row.status} for row in cleanup_runs],
        "active_lock_count": sum(
            1 for row in locks if str(row.status or "").upper() in ACTIVE_LOCK_STATUSES
        ),
        "reports": [{"id": row.id, "status": row.status} for row in reports],
        "golden": None if golden is None else {
            "status": golden.status,
            "verification_tier": golden.verification_tier,
            "score": golden.score,
            "blocker_codes": golden.blocker_codes,
            "gap_codes": golden.gap_codes,
        },
        "case_audit_event_types": sorted(case_event_set),
        "target_flow_audit_event_types": sorted(flow_event_set),
    }
    report = evaluate_signals(signals, observed=observed)
    report["schema_version"] = SCHEMA_VERSION
    report["strict_scope"] = {
        "mode": "LATEST_REPRODUCTION_MUST_BE_SINGLE_REAL_FLOW",
        "session_id": sid,
        "cross_session_mosaicking_allowed": False,
        "analyzer_requires_target_evidence_provenance": True,
    }
    report["warnings"] = []
    if observed["target_session"]["legacy_real_resolver_fallback"]:
        report["warnings"].append("PLATFORM_PROFILE_ID_LEGACY_MOCK_FALLBACK")
    if str(target.state or "").upper() not in TERMINAL_REPRO_STATES:
        report["warnings"].append("TARGET_SESSION_NOT_TERMINAL")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Strict single-session M7 real-DUT audit")
    parser.add_argument("--case", required=True, help="Case ID or case_no")
    parser.add_argument("--out", default="validation/m7_acceptance_strict_audit.json")
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()

    with SessionLocal() as db:
        case = _resolve_case(db, args.case)
        if case is None:
            report = {
                "schema_version": SCHEMA_VERSION,
                "status": "BLOCKED",
                "blocked_ids": ["M7-01"],
                "error": "CASE_NOT_FOUND",
                "requested_case": args.case,
            }
        else:
            report = collect_strict(db, case)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "schema_version": report.get("schema_version"),
        "status": report.get("status"),
        "passed": report.get("criteria_passed"),
        "total": report.get("criteria_total"),
        "blocked_ids": report.get("blocked_ids", []),
        "warnings": report.get("warnings", []),
        "strict_blockers": report.get("strict_blockers", []),
        "out": str(out),
    }, ensure_ascii=False, indent=2))
    return 2 if args.strict and report.get("status") != "PASS" else 0


if __name__ == "__main__":
    raise SystemExit(main())
