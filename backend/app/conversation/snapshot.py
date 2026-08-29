from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.conversation.planner import select_user_question
from app.conversation.state_service import ConversationStateService, slot_label
from app.db.models import AnalyzerRun, Case, DiagnosisRun, Job, ReproductionSession


_ACTIVE_JOB_STATES = {"PENDING", "RUNNING"}
_ACTIVE_DIAGNOSIS_STATES = {"PENDING", "ANALYZING", "WAITING_EVIDENCE"}
_ACTIVE_REPRO_STATES = {"CREATED", "AUTO_ARMING", "ARMED", "WATCHING", "CAPTURING", "POST_CAPTURE", "CLEANUP"}


class ConversationSnapshotBuilder:
    """Build a bounded, user-facing truth catalog for grounded replies.

    The snapshot is the only technical truth surface available to the response
    planner.  It contains deterministic runtime/diagnosis facts plus one optional
    next question selected from DiagnosisDecision.allowed user-evidence needs.
    """

    def build(self, db: Session, case_id: str) -> dict[str, Any]:
        case = db.get(Case, case_id)
        if case is None:
            raise KeyError("CASE_NOT_FOUND")
        diagnosis = db.scalar(
            select(DiagnosisRun)
            .where(DiagnosisRun.case_id == case_id)
            .order_by(DiagnosisRun.created_at.desc())
            .limit(1)
        )
        running_jobs = list(db.scalars(
            select(Job).where(Job.case_id == case_id, Job.status.in_(_ACTIVE_JOB_STATES))
        ))
        running_analyzers = list(db.scalars(
            select(AnalyzerRun).where(
                AnalyzerRun.case_id == case_id,
                AnalyzerRun.status.in_(["PENDING", "RUNNING"]),
            )
        ))
        reproduction = db.scalar(
            select(ReproductionSession)
            .where(ReproductionSession.case_id == case_id)
            .order_by(ReproductionSession.created_at.desc())
            .limit(1)
        )
        _conversation, state = ConversationStateService().case_state(db, case_id)

        summary = dict((diagnosis.summary_json or {}) if diagnosis else {})
        decision = dict((diagnosis.decision_json or {}) if diagnosis else {})
        known = list(summary.get("known") or decision.get("known") or [])[:8]
        unknown = list(summary.get("unknown") or decision.get("unknown") or [])[:8]
        blocking_reason = str(summary.get("blocking_reason") or "") or None
        manual_action = str(summary.get("manual_action") or "") or None
        headline = str(summary.get("headline") or "") or None
        active_repro = bool(reproduction and reproduction.state in _ACTIVE_REPRO_STATES)
        has_running_work = bool(
            running_jobs
            or running_analyzers
            or active_repro
            or (diagnosis and diagnosis.status in _ACTIVE_DIAGNOSIS_STATES)
        )

        facts: dict[str, str] = {
            "case_status": f"Case {case.case_no} 当前阶段：{case.status}。",
            "runtime_running": "当前仍有后台分析、采集或复现任务在运行。",
            "runtime_idle": "当前没有后台分析、采集或复现任务在运行。",
        }
        if headline:
            facts["diagnosis_headline"] = headline
        for idx, item in enumerate(known, start=1):
            facts[f"known_{idx}"] = str(item)[:700]

        uncertainties: dict[str, str] = {}
        if blocking_reason:
            uncertainties["blocking_reason"] = blocking_reason
        for idx, item in enumerate(unknown, start=1):
            uncertainties[f"unknown_{idx}"] = str(item)[:700]

        active_question = dict((state.active_question_json or {}) if state else {}) or None
        slots = dict((state.slots_json or {}) if state else {})
        unavailable = list((state.unavailable_needs_json or []) if state else [])
        if active_question and active_question.get("slot_key"):
            slot_key = str(active_question["slot_key"])
            slot_state = str((slots.get(slot_key) or {}).get("state") or "")
            if slot_state not in {"ANSWERED", "UNKNOWN_BY_USER", "UNAVAILABLE", "DECLINED", "NOT_APPLICABLE"}:
                uncertainties["active_need"] = f"仍需要：{slot_label(slot_key)}。"

        recommended = None
        if not has_running_work and diagnosis is not None:
            selected = select_user_question(
                decision=decision,
                summary=summary,
                slots=slots,
                unavailable_needs=unavailable,
            )
            if selected.kind == "QUESTION" and selected.need and selected.question:
                recommended = {
                    "id": f"recommended:{selected.need}",
                    "slot_key": selected.need,
                    "text": selected.question,
                    "fallback": selected.fallback,
                    "reason": selected.reason,
                    "score": selected.score,
                }
            elif selected.kind == "PARTIAL_CONCLUSION":
                recommended = {
                    "id": "partial-conclusion",
                    "slot_key": None,
                    "text": None,
                    "fallback": selected.fallback,
                    "reason": selected.reason,
                    "score": 0.0,
                }

        allowed_actions: dict[str, str] = {}
        if has_running_work:
            allowed_actions["WAIT_FOR_RUNNING_TASKS"] = "等待当前自动任务完成；无需重复提交相同信息。"
        else:
            if recommended and recommended.get("text"):
                allowed_actions["ANSWER_RECOMMENDED_QUESTION"] = str(recommended["text"])
                if recommended.get("fallback"):
                    allowed_actions["RECOMMENDED_QUESTION_FALLBACK"] = str(recommended["fallback"])
            if blocking_reason or active_question or (recommended and recommended.get("reason") == "NO_ASKABLE_NEED"):
                allowed_actions["PROVIDE_MISSING_EVIDENCE"] = "如果方便，可以补充当前仍有价值的缺失证据。"
                allowed_actions["FINISH_WITH_PARTIAL_CONCLUSION"] = "如果暂时无法补充，可按现有证据形成阶段结论。"
            elif case.status in {"DIAGNOSED", "ROOT_CAUSE_CONFIRMED", "RESOLVED", "CLOSED"}:
                allowed_actions["REVIEW_RESULT"] = "查看当前诊断结论和证据。"
            else:
                allowed_actions["CONTINUE_ANALYSIS"] = "继续按当前证据推进诊断。"
        if "recording" not in unavailable:
            allowed_actions.setdefault("UPLOAD_RECORDING", "如有现场录音，可上传用于时间对齐和主观现象确认。")
        if "pcap" not in unavailable:
            allowed_actions.setdefault("UPLOAD_PCAP", "如有新的异常抓包，可继续上传。")
        if "reproducibility" not in unavailable:
            allowed_actions.setdefault("REPRODUCE_WHEN_AVAILABLE", "如果现场可复现，可进入受控复现流程。")

        question_catalog = self._question_catalog(active_question, slots)
        if not question_catalog and recommended and recommended.get("text"):
            question_catalog[str(recommended["id"])] = str(recommended["text"])[:1000]

        snapshot = {
            "schema_version": "conversation-grounded-snapshot-v1",
            "case": {
                "case_id": case.id,
                "case_no": case.case_no,
                "status": case.status,
            },
            "runtime": {
                "has_running_work": has_running_work,
                "running_job_count": len(running_jobs),
                "running_analyzer_count": len(running_analyzers),
                "reproduction_state": reproduction.state if reproduction else None,
                "diagnosis_state": diagnosis.status if diagnosis else None,
            },
            "diagnosis": {
                "cycle": diagnosis.cycle if diagnosis else 0,
                "headline": headline,
                "blocking_reason": blocking_reason,
                "manual_action": manual_action,
                "known": known,
                "unknown": unknown,
            },
            "conversation": {
                "active_question": active_question,
                "recommended_question": recommended,
                "slots": slots,
                "unavailable_needs": unavailable,
            },
            "fact_catalog": facts,
            "uncertainty_catalog": uncertainties,
            "allowed_actions": allowed_actions,
            "question_catalog": question_catalog,
        }
        raw = json.dumps(snapshot, sort_keys=True, ensure_ascii=False, default=str)
        snapshot["fingerprint"] = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        return snapshot

    @staticmethod
    def _question_catalog(active_question: dict[str, Any] | None, slots: dict[str, Any]) -> dict[str, str]:
        if active_question and active_question.get("id") and active_question.get("text"):
            slot_key = str(active_question.get("slot_key") or "")
            state = str((slots.get(slot_key) or {}).get("state") or "") if slot_key else ""
            if state not in {"ANSWERED", "UNKNOWN_BY_USER", "UNAVAILABLE", "DECLINED", "NOT_APPLICABLE"}:
                return {str(active_question["id"]): str(active_question["text"])[:1000]}
        return {}
