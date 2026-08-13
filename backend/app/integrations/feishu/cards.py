from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    Case, DiagnosisRun, Hypothesis, ReproductionSession, ReproductionAttempt, ReproductionCall,
    DiagnosticExperiment, CausalAssessment, FixVerificationRun,
)


IMPORTANT_REPRO_STATES = {
    "AUTO_ARMING", "ARMED", "WATCHING", "CAPTURING", "POST_CAPTURE", "CLEANUP",
    "COMPLETED", "FAILED", "ARM_FAILED", "CLEANUP_FAILED", "CANCELLED",
}


def _plain(text: str) -> dict[str, Any]:
    return {"tag": "plain_text", "content": text}


def _md(text: str) -> dict[str, Any]:
    return {"tag": "markdown", "content": text}


def _kv_line(label: str, value: Any) -> str:
    return f"**{label}**：{value if value not in (None, '') else '-'}"


@dataclass(frozen=True)
class FeishuCaseCard:
    case_id: str
    card: dict[str, Any]


class FeishuCaseCardBuilder:
    """Builds the single mutable Case card defined by EC-12/SPEC-29.

    The builder is deterministic and read-only. It never performs device actions and never
    upgrades hypothesis state. Values are rendered from persisted backend facts only.
    """

    def build(self, db: Session, case_id: str) -> FeishuCaseCard:
        case = db.get(Case, case_id)
        if not case:
            raise KeyError("CASE_NOT_FOUND")

        diagnosis = db.scalar(
            select(DiagnosisRun).where(DiagnosisRun.case_id == case_id).order_by(DiagnosisRun.created_at.desc()).limit(1)
        )
        hypotheses = []
        if diagnosis:
            hypotheses = list(db.scalars(
                select(Hypothesis).where(Hypothesis.case_id == case_id).order_by(Hypothesis.confidence.desc())
            ))
        top_h = hypotheses[0] if hypotheses else None

        repro = db.scalar(
            select(ReproductionSession).where(ReproductionSession.case_id == case_id).order_by(ReproductionSession.created_at.desc()).limit(1)
        )
        attempts = calls = []
        if repro:
            attempts = list(db.scalars(select(ReproductionAttempt).where(ReproductionAttempt.session_id == repro.id)))
            calls = list(db.scalars(select(ReproductionCall).where(ReproductionCall.session_id == repro.id).order_by(ReproductionCall.call_no)))

        exp = db.scalar(
            select(DiagnosticExperiment).where(DiagnosticExperiment.case_id == case_id).order_by(DiagnosticExperiment.created_at.desc()).limit(1)
        )
        causal = None
        if exp:
            causal = db.scalar(
                select(CausalAssessment).where(CausalAssessment.experiment_id == exp.id).order_by(CausalAssessment.created_at.desc()).limit(1)
            )
        fix = db.scalar(
            select(FixVerificationRun).where(FixVerificationRun.case_id == case_id).order_by(FixVerificationRun.created_at.desc()).limit(1)
        )

        target_count = sum(1 for c in calls if c.role == "TARGET" or c.verdict == "MATCH")
        control_count = sum(1 for c in calls if c.role == "CONTROL" or c.verdict == "NO_MATCH")
        capture = repro.capture_completeness if repro else "-"
        suff = repro.evidence_sufficiency if repro else "-"
        cleanup = repro.cleanup_status if repro else "-"

        diagnosis_state = "尚未诊断"
        if top_h:
            state_label = {
                "OPEN": "观察中", "SUPPORTED": "支持", "STRONGLY_SUPPORTED": "强支持",
                "CONFIRMED": "已确认", "CONTRADICTED": "存在反证", "REJECTED": "已排除",
            }.get(top_h.status, top_h.status)
            diagnosis_state = f"{state_label} · {top_h.title}"
        elif diagnosis:
            diagnosis_state = diagnosis.status

        elements: list[dict[str, Any]] = [
            {"tag": "div", "text": _md(
                "\n".join([
                    _kv_line("Case", case.case_no),
                    _kv_line("状态", case.status),
                    _kv_line("问题", case.summary),
                ])
            )},
            {"tag": "hr"},
            {"tag": "div", "text": _md(
                "\n".join([
                    "**诊断进度**",
                    _kv_line("当前结论", diagnosis_state),
                    _kv_line("因果状态", causal.state if causal else "-"),
                    _kv_line("修复验证", fix.status if fix else "-"),
                ])
            )},
            {"tag": "hr"},
            {"tag": "div", "text": _md(
                "\n".join([
                    "**自动复现**",
                    _kv_line("Session", f"{repro.id[:8]} · {repro.state}" if repro else "尚未创建"),
                    _kv_line("Attempt / Call", f"{len(attempts)} / {len(calls)}"),
                    _kv_line("CONTROL / TARGET", f"{control_count} / {target_count}"),
                    _kv_line("Capture", capture),
                    _kv_line("Evidence Sufficiency", suff),
                    _kv_line("Cleanup", cleanup),
                ])
            )},
        ]

        actions = [
            {"tag": "button", "text": _plain("查看详情"), "type": "default", "value": {"action": "OPEN_CASE", "case_id": case_id}},
        ]
        if repro and repro.state not in {"COMPLETED", "FAILED", "CANCELLED"}:
            actions.append({"tag": "button", "text": _plain("停止自动复现"), "type": "danger", "value": {"action": "STOP_REPRODUCTION", "case_id": case_id, "session_id": repro.id}})
        if exp and exp.state == "WAITING_EXTERNAL_ACTION":
            actions.append({"tag": "button", "text": _plain("已完成操作"), "type": "primary", "value": {"action": "EXTERNAL_ACTION_COMPLETED", "case_id": case_id, "experiment_id": exp.id}})
        elements.append({"tag": "action", "actions": actions})

        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": self._header_template(case.status, repro.state if repro else None, fix.status if fix else None),
                "title": _plain(f"VOIP AI 故障诊断 · {case.case_no}"),
            },
            "elements": elements,
        }
        return FeishuCaseCard(case_id=case_id, card=card)

    @staticmethod
    def _header_template(case_state: str, repro_state: str | None, fix_state: str | None) -> str:
        if repro_state == "CLEANUP_FAILED" or case_state == "FAILED" or fix_state == "FIX_REGRESSION":
            return "red"
        if fix_state == "FIX_VERIFIED" or case_state in {"RESOLVED", "CLOSED"}:
            return "green"
        if repro_state in {"ARMED", "WATCHING", "CAPTURING", "AUTO_ARMING"}:
            return "blue"
        return "wathet"
