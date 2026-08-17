from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.golden_models import GoldenCandidateAssessment
from app.db.models import (
    AnalyzerRun,
    AuditLog,
    Case,
    CausalAssessment,
    DiagnosisRun,
    Evidence,
    FixVerificationRun,
    Hypothesis,
    HypothesisEvidence,
)

ASSESSMENT_VERSION = "golden-candidate-v1"
STATUSES = ("NOT_ELIGIBLE", "PARTIAL_GOLDEN", "GOLDEN_CANDIDATE", "GOLDEN_READY")
SUCCESS_RUN_STATUSES = {"SUCCESS", "PARTIAL_SUCCESS", "SUCCEEDED"}


def _utcnow():
    return datetime.now(timezone.utc)


def _count(db: Session, stmt) -> int:
    return int(db.scalar(stmt) or 0)


def _confirmed_hypotheses(db: Session, case_id: str) -> list[Hypothesis]:
    return list(
        db.scalars(
            select(Hypothesis)
            .where(Hypothesis.case_id == case_id, Hypothesis.status == "CONFIRMED")
            .order_by(Hypothesis.confidence.desc(), Hypothesis.created_at.asc())
        )
    )


def _fix_verified(db: Session, case_id: str) -> bool:
    return bool(
        db.scalar(
            select(FixVerificationRun.id)
            .where(FixVerificationRun.case_id == case_id, FixVerificationRun.status == "FIX_VERIFIED")
            .limit(1)
        )
    )


def _causal_root_confirmed(db: Session, case_id: str) -> bool:
    return bool(
        db.scalar(
            select(CausalAssessment.id)
            .where(CausalAssessment.case_id == case_id, CausalAssessment.state == "ROOT_CAUSE_CONFIRMED")
            .limit(1)
        )
    )


def _direct_l1_support(db: Session, hypotheses: list[Hypothesis]) -> bool:
    if not hypotheses:
        return False
    hypothesis_ids = [row.id for row in hypotheses]
    ref = db.scalar(
        select(HypothesisEvidence.id)
        .where(
            HypothesisEvidence.hypothesis_id.in_(hypothesis_ids),
            HypothesisEvidence.evidence_level == "L1",
            HypothesisEvidence.direction == "SUPPORT",
            HypothesisEvidence.ref_type.in_(["EVIDENCE", "ANALYZER_RUN"]),
        )
        .limit(1)
    )
    return bool(ref)


def _baseline_ready(db: Session, case_id: str) -> bool:
    row = db.scalar(
        select(DiagnosisRun)
        .where(DiagnosisRun.case_id == case_id)
        .order_by(DiagnosisRun.created_at.desc())
        .limit(1)
    )
    return bool(row and row.decision_json and isinstance(row.decision_json, dict))


def _audit_state(db: Session, case_id: str, *, has_evidence: bool, root_confirmed: bool, fix_verified: bool) -> tuple[bool, list[str], list[str]]:
    events = list(
        db.scalars(select(AuditLog.event_type).where(AuditLog.case_id == case_id))
    )
    event_set = {str(x) for x in events}
    gaps: list[str] = []
    required_groups: list[tuple[str, set[str]]] = [("AUDIT_CASE_CREATED_MISSING", {"CASE_CREATED"})]
    if has_evidence:
        required_groups.append(("AUDIT_EVIDENCE_MISSING", {"EVIDENCE_CREATED", "EVIDENCE_UPLOADED"}))
    required_groups.append(("AUDIT_DIAGNOSIS_MISSING", {"DIAGNOSIS_STARTED", "DIAGNOSIS_CYCLE", "DIAGNOSIS_UPDATED"}))
    if root_confirmed:
        required_groups.append(("AUDIT_ROOT_CAUSE_MISSING", {"HYPOTHESIS_CONFIRMED", "ROOT_CAUSE_CAUSALLY_CONFIRMED"}))
    if fix_verified:
        required_groups.append(("AUDIT_FIX_VERIFICATION_MISSING", {"FIX_VERIFICATION_UPDATED"}))
    for code, alternatives in required_groups:
        if not (event_set & alternatives):
            gaps.append(code)
    return not gaps, gaps, sorted(event_set)


def _normalize(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


def _answer_leakage(case: Case, evidences: list[Evidence], hypotheses: list[Hypothesis]) -> list[dict]:
    """Conservative leak detector.

    Symptoms are allowed in the prompt.  We only flag root-cause-like disclosure:
    exact confirmed hypothesis identifiers in filenames/metadata, or explicit root
    cause language in the Case summary coupled with a confirmed hypothesis title/code.
    Analyzer findings are intentionally not scanned because deterministic facts are
    valid model input rather than label leakage.
    """
    findings: list[dict] = []
    labels: list[tuple[str, str]] = []
    for h in hypotheses:
        code = _normalize(h.code)
        title = _normalize(h.title)
        if len(code) >= 6:
            labels.append(("code", code))
        if len(title) >= 8:
            labels.append(("title", title))

    root_markers = ("根因", "已确认", "root cause", "caused by", "原因是", "由于")
    summary = _normalize(case.summary)
    if any(marker in summary for marker in root_markers):
        for label_type, label in labels:
            if label in summary:
                findings.append({"source": "CASE_SUMMARY", "label_type": label_type, "match": label})

    for evidence in evidences:
        filename = _normalize(evidence.filename)
        metadata = _normalize(json.dumps(evidence.metadata_json or {}, ensure_ascii=False, sort_keys=True))
        for label_type, label in labels:
            if label in filename:
                findings.append({
                    "source": "EVIDENCE_FILENAME",
                    "evidence_id": evidence.id,
                    "label_type": label_type,
                    "match": label,
                })
            elif label in metadata and any(marker in metadata for marker in root_markers):
                findings.append({
                    "source": "EVIDENCE_METADATA",
                    "evidence_id": evidence.id,
                    "label_type": label_type,
                    "match": label,
                })
    return findings


def _next_steps(*, evidence_count: int, analyzer_count: int, baseline_ready: bool, root_confirmed: bool,
                direct_l1: bool, audit_complete: bool, leakage: bool, fix_verified: bool) -> list[dict]:
    steps: list[dict] = []
    if evidence_count == 0:
        steps.append({"code": "ADD_REAL_EVIDENCE", "priority": "P0", "action": "上传或自动采集当前Case的PCAP/PCM/日志等真实证据。"})
    if evidence_count and analyzer_count == 0:
        steps.append({"code": "RUN_DETERMINISTIC_ANALYZERS", "priority": "P0", "action": "对已有关联证据运行Packet/PCM/Media等确定性Analyzer。"})
    if not baseline_ready:
        steps.append({"code": "RUN_DIAGNOSIS", "priority": "P0", "action": "运行确定性Diagnosis并形成可回放baseline。"})
    if not root_confirmed:
        steps.append({"code": "CONFIRM_ROOT_CAUSE", "priority": "P0", "action": "在直接证据支持后，由现有根因确认门禁完成ROOT_CAUSE_CONFIRMED。"})
    elif not direct_l1:
        steps.append({"code": "ADD_DIRECT_L1_SUPPORT", "priority": "P0", "action": "为已确认Hypothesis补齐当前Case的L1 Evidence/AnalyzerRun支持关系。"})
    if leakage:
        steps.append({"code": "REMOVE_ANSWER_LEAKAGE", "priority": "P0", "action": "从Case summary、Evidence文件名或metadata中移除最终根因答案泄漏，再重新评估。"})
    if not audit_complete:
        steps.append({"code": "COMPLETE_AUDIT_TRAIL", "priority": "P1", "action": "补齐Case/Evidence/Diagnosis/根因确认相关审计链。"})
    if root_confirmed and not fix_verified:
        steps.append({"code": "RUN_FIX_VERIFICATION", "priority": "P2", "action": "建议完成同环境Fix Verification，将B级Golden提升为A级。"})
    return steps


class GoldenCandidateService:
    version = ASSESSMENT_VERSION

    def assess(self, db: Session, case_id: str) -> dict:
        case = db.get(Case, case_id)
        if not case:
            raise ValueError("CASE_NOT_FOUND")

        evidences = list(db.scalars(select(Evidence).where(Evidence.case_id == case_id)))
        hypotheses = _confirmed_hypotheses(db, case_id)
        evidence_count = len(evidences)
        complete_count = sum(str(x.completeness) == "COMPLETE" for x in evidences)
        l1_count = sum(str(x.level) == "L1" for x in evidences)
        analyzer_count = _count(
            db,
            select(func.count(AnalyzerRun.id)).where(
                AnalyzerRun.case_id == case_id,
                AnalyzerRun.status.in_(list(SUCCESS_RUN_STATUSES)),
            ),
        )
        fix_verified = _fix_verified(db, case_id)
        root_confirmed = bool(hypotheses) or _causal_root_confirmed(db, case_id)
        direct_l1 = _direct_l1_support(db, hypotheses)
        baseline_ready = _baseline_ready(db, case_id)

        leakage_findings = _answer_leakage(case, evidences, hypotheses)
        leakage = bool(leakage_findings)
        audit_complete, audit_gaps, audit_events = _audit_state(
            db, case_id, has_evidence=bool(evidence_count), root_confirmed=root_confirmed,
            fix_verified=fix_verified,
        )

        snapshot_ready = False
        snapshot_error: str | None = None
        if evidence_count or analyzer_count or baseline_ready:
            try:
                from app.diagnosis.snapshot import CaseEvidenceSnapshotBuilder
                CaseEvidenceSnapshotBuilder().build(db, case_id)
                snapshot_ready = True
            except Exception as exc:
                snapshot_error = f"{type(exc).__name__}:{exc}"

        blockers: list[str] = []
        gaps: list[str] = []
        if evidence_count == 0:
            gaps.append("NO_CASE_EVIDENCE")
        if analyzer_count == 0:
            gaps.append("NO_SUCCESSFUL_ANALYZER")
        if not baseline_ready:
            gaps.append("NO_DETERMINISTIC_BASELINE")
        if not root_confirmed:
            gaps.append("ROOT_CAUSE_NOT_CONFIRMED")
        if root_confirmed and not direct_l1:
            blockers.append("CONFIRMED_ROOT_CAUSE_WITHOUT_DIRECT_L1_SUPPORT")
        if not snapshot_ready:
            gaps.append("SNAPSHOT_NOT_READY")
        if not audit_complete:
            gaps.extend(audit_gaps)
        if leakage:
            blockers.append("ANSWER_LEAKAGE_RISK")

        ready = all((root_confirmed, direct_l1, baseline_ready, snapshot_ready, audit_complete)) and not leakage
        if ready:
            status = "GOLDEN_READY"
        elif root_confirmed:
            status = "GOLDEN_CANDIDATE"
        elif evidence_count or analyzer_count or baseline_ready:
            status = "PARTIAL_GOLDEN"
        else:
            status = "NOT_ELIGIBLE"

        verification_tier = "A" if fix_verified else ("B" if root_confirmed else None)
        score = 0
        score += min(20, evidence_count * 4)
        score += 15 if analyzer_count else 0
        score += 15 if baseline_ready else 0
        score += 20 if root_confirmed else 0
        score += 10 if direct_l1 else 0
        score += 10 if audit_complete else 0
        score += 10 if fix_verified else 0
        if leakage:
            score = max(0, score - 30)

        next_steps = _next_steps(
            evidence_count=evidence_count, analyzer_count=analyzer_count, baseline_ready=baseline_ready,
            root_confirmed=root_confirmed, direct_l1=direct_l1, audit_complete=audit_complete,
            leakage=leakage, fix_verified=fix_verified,
        )
        return {
            "schema_version": ASSESSMENT_VERSION,
            "case_id": case_id,
            "case_no": case.case_no,
            "status": status,
            "verification_tier": verification_tier,
            "score": min(100, score),
            "signals": {
                "root_cause_confirmed": root_confirmed,
                "fix_verified": fix_verified,
                "direct_l1_support": direct_l1,
                "deterministic_baseline_ready": baseline_ready,
                "snapshot_ready": snapshot_ready,
                "audit_coverage_complete": audit_complete,
                "answer_leakage_risk": leakage,
                "evidence_count": evidence_count,
                "complete_evidence_count": complete_count,
                "l1_evidence_count": l1_count,
                "successful_analyzer_count": analyzer_count,
                "confirmed_hypothesis_count": len(hypotheses),
            },
            "blocker_codes": sorted(set(blockers)),
            "gap_codes": sorted(set(gaps)),
            "next_steps": next_steps,
            "leakage_findings": leakage_findings,
            "details": {
                "confirmed_hypothesis_codes": [x.code for x in hypotheses],
                "confirmed_fault_domains": sorted({x.fault_domain for x in hypotheses}),
                "audit_event_types": audit_events,
                "snapshot_error": snapshot_error,
                "golden_ready_rule": "ROOT_CAUSE_CONFIRMED + DIRECT_L1_SUPPORT + DETERMINISTIC_BASELINE + SNAPSHOT + AUDIT_COMPLETE + NO_ANSWER_LEAKAGE",
                "verification_tier_rule": {"A": "FIX_VERIFIED", "B": "ROOT_CAUSE_CONFIRMED"},
            },
        }

    def refresh(self, db: Session, case_id: str, *, actor: str = "golden-candidate-engine") -> GoldenCandidateAssessment:
        result = self.assess(db, case_id)
        row = db.scalar(
            select(GoldenCandidateAssessment).where(GoldenCandidateAssessment.case_id == case_id)
        )
        old_status = row.status if row else None
        if row is None:
            row = GoldenCandidateAssessment(case_id=case_id)
            db.add(row)
        signals = result["signals"]
        row.status = result["status"]
        row.verification_tier = result["verification_tier"]
        row.assessment_version = ASSESSMENT_VERSION
        row.score = result["score"]
        row.root_cause_confirmed = int(signals["root_cause_confirmed"])
        row.fix_verified = int(signals["fix_verified"])
        row.direct_l1_support = int(signals["direct_l1_support"])
        row.deterministic_baseline_ready = int(signals["deterministic_baseline_ready"])
        row.snapshot_ready = int(signals["snapshot_ready"])
        row.audit_coverage_complete = int(signals["audit_coverage_complete"])
        row.answer_leakage_risk = int(signals["answer_leakage_risk"])
        row.evidence_count = signals["evidence_count"]
        row.complete_evidence_count = signals["complete_evidence_count"]
        row.l1_evidence_count = signals["l1_evidence_count"]
        row.successful_analyzer_count = signals["successful_analyzer_count"]
        row.confirmed_hypothesis_count = signals["confirmed_hypothesis_count"]
        row.blocker_codes = result["blocker_codes"]
        row.gap_codes = result["gap_codes"]
        row.next_steps = result["next_steps"]
        row.leakage_findings = result["leakage_findings"]
        row.details_json = result["details"]
        row.assessed_at = _utcnow()
        row.updated_at = _utcnow()
        db.flush()

        if old_status != row.status:
            from app.services.audit import audit
            audit(
                db,
                case_id=case_id,
                actor=actor,
                event_type="GOLDEN_CANDIDATE_STATE_CHANGED",
                target_type="golden_candidate_assessment",
                target_id=row.id,
                before_json={"status": old_status} if old_status else None,
                after_json={
                    "status": row.status,
                    "verification_tier": row.verification_tier,
                    "score": row.score,
                },
                detail={
                    "assessment_version": ASSESSMENT_VERSION,
                    "blocker_codes": row.blocker_codes,
                    "gap_codes": row.gap_codes,
                },
            )
        return row

    @staticmethod
    def as_dict(row: GoldenCandidateAssessment) -> dict:
        return {
            "id": row.id,
            "case_id": row.case_id,
            "status": row.status,
            "verification_tier": row.verification_tier,
            "assessment_version": row.assessment_version,
            "score": row.score,
            "signals": {
                "root_cause_confirmed": bool(row.root_cause_confirmed),
                "fix_verified": bool(row.fix_verified),
                "direct_l1_support": bool(row.direct_l1_support),
                "deterministic_baseline_ready": bool(row.deterministic_baseline_ready),
                "snapshot_ready": bool(row.snapshot_ready),
                "audit_coverage_complete": bool(row.audit_coverage_complete),
                "answer_leakage_risk": bool(row.answer_leakage_risk),
                "evidence_count": row.evidence_count,
                "complete_evidence_count": row.complete_evidence_count,
                "l1_evidence_count": row.l1_evidence_count,
                "successful_analyzer_count": row.successful_analyzer_count,
                "confirmed_hypothesis_count": row.confirmed_hypothesis_count,
            },
            "blocker_codes": row.blocker_codes or [],
            "gap_codes": row.gap_codes or [],
            "next_steps": row.next_steps or [],
            "leakage_findings": row.leakage_findings or [],
            "details": row.details_json or {},
            "assessed_at": row.assessed_at,
            "updated_at": row.updated_at,
        }
