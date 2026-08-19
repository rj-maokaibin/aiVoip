from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.ai_intelligence_models import AIDiagnosticCycle
from app.db.models import (
    Case, DiagnosisRun, Hypothesis, ReproductionSession, ReproductionAttempt, ReproductionCall,
    CaptureChannelHealth, DiagnosticExperiment, CausalAssessment, FixVerificationRun,
)
from app.db.evidence_report_models import FeishuEvidenceDocumentBinding, PreliminaryEvidenceReport


IMPORTANT_REPRO_STATES = {
    "AUTO_ARMING", "ARMED", "WATCHING", "CAPTURING", "POST_CAPTURE", "CLEANUP",
    "COMPLETED", "FAILED", "ARM_FAILED", "CLEANUP_FAILED", "CANCELLED",
}


def _plain(text: str) -> dict[str, Any]:
    return {"tag": "plain_text", "content": text}


def _md(text: str) -> dict[str, Any]:
    return {"tag": "lark_md", "content": text}


def _kv_line(label: str, value: Any) -> str:
    return f"**{label}**：{value if value not in (None, '') else '-'}"


def _ai2_kind_label(kind: str) -> str:
    return {
        "QUESTION": "诊断问题",
        "REPRODUCTION_PROFILE": "注册复现 Profile",
        "EXPERIMENT_PROFILE": "注册 A/B Experiment",
        "USER_EVIDENCE_REQUEST": "补充现场证据",
    }.get(kind, kind or "-")


def _ai2_state_label(cycle: AIDiagnosticCycle) -> str:
    state = cycle.suggestion_state or "NONE"
    if state == "DISPATCHED":
        return "已采纳并进入确定性工作流"
    if (
        state == "ACCEPTED"
        and cycle.execution_ref_type == "reproduction_session"
        and cycle.execution_ref_id
    ):
        return "复现 Session 已创建，等待/重试任务投递"
    return state


def _ai2_retryable(cycle: AIDiagnosticCycle) -> bool:
    state = cycle.suggestion_state or "NONE"
    if state == "PROPOSED":
        return True
    return bool(
        state == "ACCEPTED"
        and cycle.execution_ref_type == "reproduction_session"
        and cycle.execution_ref_id
    )


@dataclass(frozen=True)
class FeishuCaseCard:
    case_id: str
    card: dict[str, Any]


class FeishuCaseCardBuilder:
    """Deterministic single mutable Case card. It never upgrades diagnosis authority."""

    def build(self, db: Session, case_id: str) -> FeishuCaseCard:
        case = db.get(Case, case_id)
        if not case:
            raise KeyError("CASE_NOT_FOUND")

        diagnosis = db.scalar(select(DiagnosisRun).where(DiagnosisRun.case_id == case_id).order_by(DiagnosisRun.created_at.desc()).limit(1))
        hypotheses = list(db.scalars(select(Hypothesis).where(Hypothesis.case_id == case_id).order_by(Hypothesis.confidence.desc()))) if diagnosis else []
        top_h = hypotheses[0] if hypotheses else None
        repro = db.scalar(select(ReproductionSession).where(ReproductionSession.case_id == case_id).order_by(ReproductionSession.created_at.desc()).limit(1))
        attempts = list(db.scalars(select(ReproductionAttempt).where(ReproductionAttempt.session_id == repro.id))) if repro else []
        calls = list(db.scalars(select(ReproductionCall).where(ReproductionCall.session_id == repro.id).order_by(ReproductionCall.call_no))) if repro else []
        exp = db.scalar(select(DiagnosticExperiment).where(DiagnosticExperiment.case_id == case_id).order_by(DiagnosticExperiment.created_at.desc()).limit(1))
        causal = db.scalar(select(CausalAssessment).where(CausalAssessment.experiment_id == exp.id).order_by(CausalAssessment.created_at.desc()).limit(1)) if exp else None
        fix = db.scalar(select(FixVerificationRun).where(FixVerificationRun.case_id == case_id).order_by(FixVerificationRun.created_at.desc()).limit(1))
        evidence_report = db.scalar(select(PreliminaryEvidenceReport).where(
            PreliminaryEvidenceReport.case_id == case_id,
            PreliminaryEvidenceReport.scope_type == "CASE",
            PreliminaryEvidenceReport.status.in_(["COMPLETE", "PARTIAL_COMPLETE"]),
        ).order_by(PreliminaryEvidenceReport.version.desc()).limit(1))
        evidence_doc = db.scalar(select(FeishuEvidenceDocumentBinding).where(FeishuEvidenceDocumentBinding.case_id == case_id).limit(1))
        ai2_cycle = db.scalar(
            select(AIDiagnosticCycle).where(
                AIDiagnosticCycle.case_id == case_id,
                AIDiagnosticCycle.runtime_stage == "SUGGEST",
                AIDiagnosticCycle.status == "COMPLETED",
            ).order_by(AIDiagnosticCycle.cycle_no.desc(), AIDiagnosticCycle.created_at.desc()).limit(1)
        )
        ai2_action = dict(ai2_cycle.next_action_json or {}) if ai2_cycle else {}

        target_count = sum(1 for c in calls if c.role == "TARGET" or c.verdict == "MATCH")
        control_count = sum(1 for c in calls if c.role == "CONTROL" or c.verdict == "NO_MATCH")
        capture = repro.capture_completeness if repro else "-"
        suff = repro.evidence_sufficiency if repro else "-"
        cleanup = repro.cleanup_status if repro else "-"
        operation_status = "尚未创建复现任务"
        if repro:
            debug_health = db.scalar(select(CaptureChannelHealth).where(CaptureChannelHealth.session_id == repro.id, CaptureChannelHealth.channel == "DEBUG"))
            debug_details = (debug_health.health_json if debug_health else {}) or {}
            if repro.state in {"COMPLETED", "FAILED", "CANCELLED", "CLEANUP_FAILED"}:
                operation_status = "复现流程已结束"
            elif debug_health and (debug_health.status == "FAILED" or debug_details.get("runtime_ready") is False):
                operation_status = "禁止继续操作：FXS 监听未就绪或已失败"
            elif debug_details.get("runtime_ready") is True:
                operation_status = "可以开始现场复现：FXS 监听已就绪"
            else:
                operation_status = "请等待 FXS_MONITOR_READY，暂勿操作话机"

        diagnosis_state = "尚未诊断"
        if top_h:
            state_label = {"OPEN":"观察中","SUPPORTED":"支持","STRONGLY_SUPPORTED":"强支持","CONFIRMED":"已确认","CONTRADICTED":"存在反证","REJECTED":"已排除"}.get(top_h.status, top_h.status)
            diagnosis_state = f"{state_label} · {top_h.title}"
        elif diagnosis:
            diagnosis_state = diagnosis.status

        report_payload = (evidence_report.snapshot_json if evidence_report else {}) or {}
        top_findings = report_payload.get("findings", [])[:3]
        report_lines = [
            "**初步证据分析**",
            _kv_line("报告", f"V{evidence_report.version} · {evidence_report.status}" if evidence_report else "尚未生成"),
            _kv_line("证据完整度", ((report_payload.get("completeness") or {}).get("state") if evidence_report else "-")),
            _kv_line("Finding", report_payload.get("finding_count") if evidence_report else 0),
            _kv_line("最高等级", report_payload.get("highest_severity") if evidence_report else "-"),
        ]
        for idx, finding in enumerate(top_findings, start=1):
            report_lines.append(f"**{idx}. {finding.get('severity','INFO')}｜{finding.get('title','')}**")
        if evidence_report:
            report_lines.append("_以上仅为 Evidence Finding（初步证据问题点），不等于最终 Root Cause（根因）。_")

        elements: list[dict[str, Any]] = [
            {"tag":"div","text":_md("\n".join([_kv_line("Case",case.case_no),_kv_line("状态",case.status),_kv_line("问题",case.summary)]))},
            {"tag":"hr"},
            {"tag":"div","text":_md("\n".join(report_lines))},
            {"tag":"hr"},
            {"tag":"div","text":_md("\n".join(["**诊断进度**",_kv_line("当前结论",diagnosis_state),_kv_line("因果状态",causal.state if causal else "-"),_kv_line("修复验证",fix.status if fix else "-")]))},
        ]

        if ai2_cycle and ai2_action:
            ai2_lines = [
                "**AI2 下一步建议（SUGGEST）**",
                _kv_line("类型", _ai2_kind_label(str(ai2_action.get("type") or ""))),
                _kv_line("注册 ID", ai2_action.get("registered_id")),
                _kv_line("理由", ai2_action.get("reason")),
                _kv_line("状态", _ai2_state_label(ai2_cycle)),
                "_这是 AI 建议，不是 Root Cause；AI 不自动执行。点击采纳后仍会重新经过用户 RBAC、Case ACL、Registry 与确定性 Orchestrator。_",
            ]
            elements.extend([
                {"tag":"hr"},
                {"tag":"div","text":_md("\n".join(ai2_lines))},
            ])

        elements.extend([
            {"tag":"hr"},
            {"tag":"div","text":_md("\n".join(["**自动复现**",_kv_line("Session",f"{repro.id[:8]} · {repro.state}" if repro else "尚未创建"),_kv_line("Attempt / Call",f"{len(attempts)} / {len(calls)}"),_kv_line("CONTROL / TARGET",f"{control_count} / {target_count}"),_kv_line("Capture",capture),_kv_line("Evidence Sufficiency",suff),_kv_line("Cleanup",cleanup),_kv_line("现场操作",operation_status)]))},
        ])

        actions = [{"tag":"button","text":_plain("查看详情"),"type":"default","value":{"action":"OPEN_CASE","case_id":case_id}}]
        if evidence_doc and evidence_doc.document_url:
            actions.insert(0,{"tag":"button","text":_plain("查看完整证据报告"),"type":"primary","url":evidence_doc.document_url})
        if ai2_cycle and ai2_action and _ai2_retryable(ai2_cycle):
            retrying = (ai2_cycle.suggestion_state or "NONE") == "ACCEPTED"
            actions.append({
                "tag":"button",
                "text":_plain("重试 AI2 任务投递" if retrying else "采纳 AI2 建议"),
                "type":"primary",
                "value":{"action":"AI2_ACCEPT_SUGGESTION","case_id":case_id,"cycle_id":ai2_cycle.id},
            })
        if repro and repro.state not in {"COMPLETED", "FAILED", "CANCELLED"}:
            actions.append({"tag":"button","text":_plain("停止自动复现"),"type":"danger","value":{"action":"STOP_REPRODUCTION","case_id":case_id,"session_id":repro.id}})
        if exp and exp.state == "WAITING_EXTERNAL_ACTION":
            actions.append({"tag":"button","text":_plain("已完成操作"),"type":"primary","value":{"action":"EXTERNAL_ACTION_COMPLETED","case_id":case_id,"experiment_id":exp.id}})
        elements.append({"tag":"action","actions":actions})

        card = {"config":{"wide_screen_mode":True},"header":{"template":self._header_template(case.status,repro.state if repro else None,fix.status if fix else None),"title":_plain(f"VOIP AI 故障诊断 · {case.case_no}")},"elements":elements}
        return FeishuCaseCard(case_id=case_id, card=card)

    @staticmethod
    def _header_template(case_state: str, repro_state: str | None, fix_state: str | None) -> str:
        if repro_state == "CLEANUP_FAILED" or case_state == "FAILED" or fix_state == "FIX_REGRESSION": return "red"
        if fix_state == "FIX_VERIFIED" or case_state in {"RESOLVED", "CLOSED"}: return "green"
        if repro_state in {"ARMED", "WATCHING", "CAPTURING", "AUTO_ARMING"}: return "blue"
        return "wathet"