#!/usr/bin/env python3
"""M7 real-DUT closed-loop acceptance gate.

This gate is deliberately READ-ONLY. It never SSHes to a DUT and never starts,
stops, or mutates a reproduction session. The normal VOIP Case/Reproduction flow
owns device control; M7 only verifies that a completed lab/field Case left a
machine-auditable end-to-end evidence trail.

M7 validates flow readiness, not model-quality promotion and not root-cause truth.
A Case may pass M7 while its Golden Candidate status is PARTIAL_GOLDEN or
GOLDEN_CANDIDATE. GOLDEN_READY and AI production promotion remain separate gates.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

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

SCHEMA_VERSION = "m7-real-dut-acceptance-v1"
SUCCESS_RUN_STATUSES = {"SUCCESS", "PARTIAL_SUCCESS", "SUCCEEDED"}
VERIFIED_CLEANUP_STATUSES = {"CLEANUP_VERIFIED", "CLEANUP_VERIFIED_EXTERNAL_WAIT"}
TERMINAL_CALL_STATUSES = {"ENDED", "ANALYZING", "ANALYZED"}
ACTIVE_LOCK_STATUSES = {"ACTIVE", "QUARANTINED"}

CRITERIA = (
    ("M7-01", "case_exists", "Case 已建立", "通过正常入口创建并持久化 Case。"),
    ("M7-02", "dut_bound", "DUT 已绑定", "为 Case 绑定被测 VOIP 设备。"),
    ("M7-03", "voice_context_ready", "Voice VLAN/接口/Gateway 上下文有效", "运行真实平台 voice runtime context 解析并确认接口 UP。"),
    ("M7-04", "pcap_present", "PCAP 证据存在", "通过 Case/Reproduction 流程采集并保留 PCAP。"),
    ("M7-05", "pcm_rx_present", "PCM RX 证据存在", "启用并保留 PCM RX 采集。"),
    ("M7-06", "pcm_tx_present", "PCM TX 证据存在", "启用并保留 PCM TX 采集。"),
    ("M7-07", "debug_present", "Debug/日志证据存在", "启用必要 debug/log 并在结束后形成可追踪证据。"),
    ("M7-08", "packet_analyzer_success", "Packet/SIP/RTP Analyzer 成功", "对 PCAP 运行确定性 Packet/SIP/RTP Analyzer。"),
    ("M7-09", "media_analyzer_success", "PCM/Media Analyzer 成功", "对 PCM/Media 运行确定性 Analyzer。"),
    ("M7-10", "deterministic_diagnosis_ready", "确定性 Diagnosis baseline 已形成", "运行 Diagnosis 直到形成 decision_json baseline。"),
    ("M7-11", "ai_shadow_present", "AI SHADOW 已实际运行", "配置真实 Reasoning Gateway，并保持 AI_PROMOTION_STAGE=SHADOW。"),
    ("M7-12", "ai_grounded", "AI Proposal 通过 grounding/contract 校验", "修复 Evidence 引用、Schema 或注册项问题，使至少一个 SHADOW proposal ACCEPTED。"),
    ("M7-13", "ai_authority_safe", "AI 未越权改变正式诊断", "确保 ACCEPTED AI hypothesis 仍为 L5/OPEN/non-confirmable 且 formal_result_changed=false。"),
    ("M7-14", "reproduction_armed", "真实平台自动复现已成功 ARMED/WATCHING", "由 real Reproduction Platform 和现有 Orchestrator 完成 arm validation。"),
    ("M7-15", "call_detected", "真实 Call 已识别、绑定并结束", "完成一次真实拨号/通话，使 Call 生命周期可审计。"),
    ("M7-16", "cleanup_verified", "临时采集状态 Cleanup Verified", "让 Orchestrator 完成 PCM/debug/tcpdump cleanup 并校验。"),
    ("M7-17", "no_active_lock", "无残留诊断锁", "清理 ACTIVE/QUARANTINED DeviceDiagnosticLock。"),
    ("M7-18", "report_generated", "诊断报告已生成", "生成 DiagnosisReport，至少包含 JSON/HTML 对象。"),
    ("M7-19", "golden_materialized", "Golden Candidate 已自动沉淀", "等待自动评估 sidecar 或对单 Case 执行 refresh；不要求 GOLDEN_READY。"),
    ("M7-20", "audit_complete", "M7 核心 Audit 链完整", "补齐 Case/Evidence/Analyzer/Diagnosis/AI/Reproduction/Call/Cleanup/Report 审计事件。"),
)


def evaluate_signals(signals: dict[str, bool], *, observed: dict[str, Any] | None = None) -> dict[str, Any]:
    """Pure evaluator used by CLI and unit tests."""
    rows = []
    for criterion_id, key, title, remediation in CRITERIA:
        passed = bool(signals.get(key, False))
        rows.append(
            {
                "id": criterion_id,
                "key": key,
                "title": title,
                "mandatory": True,
                "status": "PASS" if passed else "BLOCKED",
                "remediation": None if passed else remediation,
            }
        )
    blocked = [row for row in rows if row["mandatory"] and row["status"] != "PASS"]
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "PASS" if not blocked else "BLOCKED",
        "promotion_eligible": False,
        "root_cause_confirmation_required_for_m7": False,
        "criteria_total": len(rows),
        "criteria_passed": len(rows) - len(blocked),
        "criteria_blocked": len(blocked),
        "blocked_ids": [row["id"] for row in blocked],
        "criteria": rows,
        "observed": observed or {},
        "notes": [
            "M7 validates one real-DUT diagnostic flow; it does not certify model quality.",
            "Golden READY / ROOT_CAUSE_CONFIRMED / FIX_VERIFIED are independent maturity gates.",
            "CONTROLLED_PLANNER remains governed by ai-promotion-gate-v1.",
        ],
    }


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


def _audit_group(event_set: set[str], alternatives: set[str]) -> bool:
    return bool(event_set & alternatives)


def _is_real_session(row: ReproductionSession) -> bool:
    """A reproduction is "real" when it actually drove a real DUT, not the Mock
    platform.  The V1 real platform records platform_profile_id ``ruijie-voip-
    aim-real``; the V2 production platform records ``ruijie-voip-capture-v2``.
    ``create_session`` snapshots the default Mock platform id, so also accept a
    REAL voice-context resolver (``REAL_VOICE_CONTEXT_V1``) which is only
    produced by the real platform ARM path.
    """
    profile = str(getattr(row, "platform_profile_id", "") or "").lower()
    if profile and "mock" not in profile:
        return True
    ctx = getattr(row, "voice_runtime_context_json", None) or {}
    resolver = str(ctx.get("resolver_id") or "").upper()
    return "REAL" in resolver


def _ai_authority_safe(proposals: list[AIProposalRecord]) -> bool:
    accepted = [row for row in proposals if row.mode == "SHADOW" and row.status == "ACCEPTED"]
    if not accepted:
        return False
    for row in accepted:
        if (row.diff_json or {}).get("formal_result_changed") is not False:
            return False
        validated = row.validated_output_json or {}
        for hypothesis in validated.get("hypotheses") or []:
            if hypothesis.get("confirmable") is not False:
                return False
            if hypothesis.get("evidence_level") != "L5":
                return False
            if str(hypothesis.get("status") or "").upper() == "CONFIRMED":
                return False
        for claim in validated.get("claims") or []:
            if claim.get("evidence_level") != "L5":
                return False
            if str(claim.get("status") or "").upper() != "PROPOSED":
                return False
    return True


def collect_case_signals(db: Session, case: Case) -> tuple[dict[str, bool], dict[str, Any]]:
    case_id = case.id
    devices = list(db.scalars(select(CaseDevice).where(CaseDevice.case_id == case_id)))
    evidences = list(db.scalars(select(Evidence).where(Evidence.case_id == case_id)))
    analyzers = list(db.scalars(select(AnalyzerRun).where(AnalyzerRun.case_id == case_id)))
    diagnoses = list(
        db.scalars(select(DiagnosisRun).where(DiagnosisRun.case_id == case_id).order_by(DiagnosisRun.created_at.desc()))
    )
    proposals = list(
        db.scalars(select(AIProposalRecord).where(AIProposalRecord.case_id == case_id).order_by(AIProposalRecord.created_at.desc()))
    )
    sessions = list(
        db.scalars(select(ReproductionSession).where(ReproductionSession.case_id == case_id).order_by(ReproductionSession.created_at.desc()))
    )
    real_sessions = [row for row in sessions if _is_real_session(row)]
    session_ids = [row.id for row in sessions]
    real_session_ids = {row.id for row in real_sessions}
    device_ids = [row.id for row in devices]

    def _by_sessions(model):
        if not session_ids:
            return []
        return list(db.scalars(select(model).where(model.session_id.in_(session_ids))))

    voice_contexts = list(db.scalars(select(VoiceRuntimeContextSnapshot).where(VoiceRuntimeContextSnapshot.case_id == case_id)))
    segments = _by_sessions(ReproductionCaptureSegment)
    arm_results = _by_sessions(ArmValidationResult)
    cleanup_runs = _by_sessions(CleanupRun)
    channel_health = _by_sessions(CaptureChannelHealth)
    calls = list(db.scalars(select(ReproductionCall).where(ReproductionCall.case_id == case_id)))
    reports = list(db.scalars(select(DiagnosisReport).where(DiagnosisReport.case_id == case_id)))
    audit_rows = list(db.scalars(select(AuditLog).where(AuditLog.case_id == case_id)))
    outbox_types = set(
        db.scalars(select(EventOutbox.event_type).where(EventOutbox.case_id == case_id))
    )
    # Audit events are recorded across two stores: ``AuditLog`` (analyzers,
    # diagnosis, reproduction state) and ``EventOutbox`` (arm/cleanup validation,
    # media milestones).  M7 verifies the audit CHAIN, so both sources count.
    event_set = {str(row.event_type) for row in audit_rows} | {str(x) for x in outbox_types}
    golden = db.scalar(select(GoldenCandidateAssessment).where(GoldenCandidateAssessment.case_id == case_id))

    locks: list[DeviceDiagnosticLock] = []
    if session_ids:
        locks.extend(db.scalars(select(DeviceDiagnosticLock).where(DeviceDiagnosticLock.session_id.in_(session_ids))))
    if device_ids:
        known = {row.id for row in locks}
        for row in db.scalars(select(DeviceDiagnosticLock).where(DeviceDiagnosticLock.device_id.in_(device_ids))):
            if row.id not in known:
                locks.append(row)

    segment_channels = {str(row.channel or "").upper() for row in segments}
    # Capture V2 captures PCM RX/TX and debug/log inside the same PCAP data plane;
    # the authoritative per-channel evidence is CaptureChannelHealth (packet counts
    # >0 for PCM after a real call, HEALTHY for DEBUG/LOG).
    v2_pcm_rx = any(
        str(row.channel or "").upper() == "PCM_RX" and int(row.packet_count or 0) > 0
        for row in channel_health
    )
    v2_pcm_tx = any(
        str(row.channel or "").upper() == "PCM_TX" and int(row.packet_count or 0) > 0
        for row in channel_health
    )
    v2_debug = any(
        str(row.channel or "").upper() in {"DEBUG", "LOG"}
        and str(row.status or "").upper() == "HEALTHY"
        for row in channel_health
    )
    pcap_present = "PCAP" in segment_channels or _has_token(evidences, "PCAP", "PCAPNG")
    pcm_rx_present = "PCM_RX" in segment_channels or _has_token(evidences, "PCM_RX", "PCM RX") or v2_pcm_rx
    pcm_tx_present = "PCM_TX" in segment_channels or _has_token(evidences, "PCM_TX", "PCM TX") or v2_pcm_tx
    debug_present = bool(segment_channels & {"DEBUG", "LOG"}) or _has_token(evidences, "DEBUG", "AIM.LOG", "AIM_LOG", "SYSLOG") or v2_debug

    successful = [row for row in analyzers if str(row.status).upper() in SUCCESS_RUN_STATUSES]
    packet_analyzer_success = any(
        any(token in str(row.analyzer_name or "").upper() for token in ("PACKET", "PCAP", "SIP", "RTP"))
        for row in successful
    )
    media_analyzer_success = any(
        any(token in str(row.analyzer_name or "").upper() for token in ("PCM", "MEDIA", "AUDIO", "RTP"))
        for row in successful
    )

    diagnosis = next(
        (
            row
            for row in diagnoses
            if isinstance(row.decision_json, dict)
            and bool(row.decision_json)
            and str(row.status).upper() not in {"FAILED", "UNAVAILABLE"}
        ),
        None,
    )
    shadow = [row for row in proposals if row.mode == "SHADOW"]
    accepted = [row for row in shadow if row.status == "ACCEPTED"]
    ai_grounded = any(
        isinstance(row.validated_output_json, dict)
        and not (row.validation_errors or [])
        for row in accepted
    )

    voice_context_ready = any(
        row.session_id in real_session_ids
        and bool(row.interface_up and row.voice_interface and row.voice_gateway_ip)
        for row in voice_contexts
    )
    real_arm_results = [row for row in arm_results if row.session_id in real_session_ids]
    reproduction_armed = bool(real_sessions) and (
        any(str(row.status).upper() == "PASSED" for row in real_arm_results)
        or "REPRODUCTION_ARM_VALIDATED" in event_set
    )
    call_detected = any(
        row.session_id in real_session_ids
        and (
            bool(row.ended_at)
            or str(row.status or "").upper() in TERMINAL_CALL_STATUSES
            or bool(row.quick_analysis_json)
        )
        for row in calls
    )

    cleanup_required_sessions = [row for row in real_sessions if bool(row.cleanup_required)]
    # After a successful cleanup the session flips cleanup_required back to False,
    # so a completed real session must be considered verified when it shows a
    # VERIFIED cleanup status (and, when cleanup runs exist, at least one VERIFIED
    # CleanupRun).  Only sessions still marked cleanup_required need all-of-them
    # verified.
    real_verified = any(
        str(row.cleanup_status or "").upper() in VERIFIED_CLEANUP_STATUSES for row in real_sessions
    )
    cleanup_verified = (
        (not cleanup_required_sessions and real_verified)
        or bool(cleanup_required_sessions)
        and all(
            str(row.cleanup_status or "").upper() in VERIFIED_CLEANUP_STATUSES
            for row in cleanup_required_sessions
        )
    )
    real_cleanup_runs = [row for row in cleanup_runs if row.session_id in real_session_ids]
    if real_cleanup_runs:
        cleanup_verified = cleanup_verified and any(str(row.status).upper() == "VERIFIED" for row in real_cleanup_runs)

    no_active_lock = not any(str(row.status or "").upper() in ACTIVE_LOCK_STATUSES for row in locks)
    report_generated = any(
        str(row.status or "").upper() == "GENERATED"
        and bool(row.html_object_key)
        and bool(row.json_object_key)
        for row in reports
    )

    audit_complete = all(
        [
            _audit_group(event_set, {"CASE_CREATED"}),
            _audit_group(event_set, {"EVIDENCE_CREATED", "EVIDENCE_UPLOADED"}),
            _audit_group(event_set, {"ANALYZER_COMPLETED", "PACKET_ANALYSIS_FINISHED", "MEDIA_ANALYSIS_FINISHED", "PCM_ANALYSIS_FINISHED"}),
            _audit_group(event_set, {"DIAGNOSIS_STARTED", "DIAGNOSIS_CYCLE", "DIAGNOSIS_UPDATED"}),
            _audit_group(event_set, {"AI_PROPOSAL_EVALUATED"}),
            _audit_group(event_set, {"REPRODUCTION_CREATED", "REPRODUCTION_STATE_CHANGED"}),
            _audit_group(event_set, {"REPRODUCTION_ARM_VALIDATED"}),
            _audit_group(event_set, {"REPRODUCTION_CALL_CHANGED", "TARGET_CONFIRMED"}),
            _audit_group(event_set, {"REPRODUCTION_CLEANUP_VALIDATED"}),
            _audit_group(event_set, {"REPORT_READY", "DIAGNOSIS_REPORT_GENERATED"}),
        ]
    )

    signals = {
        "case_exists": True,
        "dut_bound": bool(devices),
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
        "devices": [
            {"id": row.id, "sn": row.sn, "platform_id": row.platform_id, "ssh_port": row.ssh_port}
            for row in devices
        ],
        "evidence": {
            "count": len(evidences),
            "types": sorted({str(row.type) for row in evidences}),
            "segment_channels": sorted(segment_channels),
        },
        "analyzers": [
            {"id": row.id, "name": row.analyzer_name, "status": row.status, "version": row.analyzer_version}
            for row in analyzers
        ],
        "diagnosis": None
        if diagnosis is None
        else {"id": diagnosis.id, "status": diagnosis.status, "reasoner": diagnosis.reasoner_name, "workflow_version": diagnosis.workflow_version},
        "ai_shadow": [
            {"id": row.id, "status": row.status, "model": row.model_name, "prompt_version": row.prompt_version, "validation_errors": row.validation_errors or []}
            for row in shadow[:10]
        ],
        "real_platform_session_count": len(real_sessions),
        "reproduction": [
            {"id": row.id, "state": row.state, "profile": row.profile_key, "platform_profile_id": row.platform_profile_id, "cleanup_status": row.cleanup_status, "capture_completeness": row.capture_completeness}
            for row in sessions[:10]
        ],
        "calls": [{"id": row.id, "status": row.status, "verdict": row.verdict, "role": row.role} for row in calls[:20]],
        "cleanup_runs": [{"id": row.id, "status": row.status} for row in cleanup_runs[:20]],
        "active_lock_count": sum(1 for row in locks if str(row.status or "").upper() in ACTIVE_LOCK_STATUSES),
        "reports": [{"id": row.id, "status": row.status} for row in reports[:10]],
        "golden": None
        if golden is None
        else {
            "status": golden.status,
            "verification_tier": golden.verification_tier,
            "score": golden.score,
            "blocker_codes": golden.blocker_codes,
            "gap_codes": golden.gap_codes,
        },
        "audit_event_types": sorted(event_set),
    }
    return signals, observed


def _markdown(report: dict[str, Any]) -> str:
    obs = report.get("observed") or {}
    case = obs.get("case") or {}
    lines = [
        "# M7 真实 DUT 智能诊断闭环验收结果",
        "",
        f"- **Case**: `{case.get('case_no', '-')}` (`{case.get('id', '-')}`)",
        f"- **M7 状态**: **{report['status']}**",
        f"- **通过**: {report['criteria_passed']}/{report['criteria_total']}",
        "- **说明**: M7 只验证真实 DUT 诊断闭环，不等同于 Root Cause/Golden READY/AI Promotion PASS。",
        "",
        "## 验收项",
        "",
        "| ID | 验收项 | 状态 | 缺口处理 |",
        "|---|---|---|---|",
    ]
    for row in report["criteria"]:
        remediation = (row.get("remediation") or "-").replace("|", "\\|")
        lines.append(f"| {row['id']} | {row['title']} | {row['status']} | {remediation} |")
    lines += ["", "## 当前 Golden 状态", "", "```json", json.dumps(obs.get("golden"), ensure_ascii=False, indent=2), "```", ""]
    return "\n".join(lines)


def _resolve_case(db: Session, value: str) -> Case | None:
    row = db.get(Case, value)
    if row:
        return row
    return db.scalar(select(Case).where(Case.case_no == value).limit(1))


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only M7 real-DUT acceptance gate")
    parser.add_argument("--case", required=True, help="Case ID or case_no")
    parser.add_argument("--out-json", default="validation/m7_acceptance_report.json")
    parser.add_argument("--out-md", default="validation/m7_acceptance_report.md")
    parser.add_argument("--strict", action="store_true", help="exit non-zero when M7 is BLOCKED")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        case = _resolve_case(db, args.case)
        if not case:
            signals = {key: False for _, key, _, _ in CRITERIA}
            report = evaluate_signals(signals, observed={"requested_case": args.case, "error": "CASE_NOT_FOUND"})
        else:
            signals, observed = collect_case_signals(db, case)
            report = evaluate_signals(signals, observed=observed)
    finally:
        db.close()

    out_json = Path(args.out_json)
    out_md = Path(args.out_md)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    out_md.write_text(_markdown(report), encoding="utf-8")
    print(json.dumps({
        "schema_version": report["schema_version"],
        "status": report["status"],
        "passed": report["criteria_passed"],
        "total": report["criteria_total"],
        "blocked_ids": report["blocked_ids"],
        "out_json": str(out_json),
        "out_md": str(out_md),
    }, ensure_ascii=False, indent=2))
    return 2 if args.strict and report["status"] != "PASS" else 0


if __name__ == "__main__":
    raise SystemExit(main())
