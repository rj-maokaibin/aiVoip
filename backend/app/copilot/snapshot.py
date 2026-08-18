from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.contracts.enums import UserRole
from app.db.evidence_report_models import EvidenceFinding, PreliminaryEvidenceReport
from app.db.models import (
    Case, DiagnosisRun, DiagnosticExperiment, FixVerificationRun, ReproductionSession,
)
from app.diagnosis.snapshot import CaseEvidenceSnapshotBuilder


_RAW_ROLES = {UserRole.ENGINEER, UserRole.EXPERT_REVIEWER, UserRole.ADMIN, UserRole.SERVICE}


def _dt(value):
    return value.isoformat() if value is not None else None


def _safe_attr(row, name: str, default=None):
    return getattr(row, name, default)


class CaseIntelligenceSnapshotBuilder:
    """Build one current-Case-only, role-aware source of truth for AI3.

    Viewer receives report-level Evidence summaries only. Roles allowed to inspect
    engineering Evidence receive the existing CaseEvidenceSnapshotBuilder output.
    No cross-Case historical data is included in AI3 V1; this prevents chat Q&A
    from accidentally turning similarity/RAG context into current-Case fact.
    """

    schema_version = "case-intelligence-snapshot-v1"

    def __init__(self, evidence_builder: CaseEvidenceSnapshotBuilder | None = None):
        self.evidence_builder = evidence_builder or CaseEvidenceSnapshotBuilder()

    def build(self, db: Session, case_id: str, *, role: UserRole) -> dict[str, Any]:
        case = db.get(Case, case_id)
        if case is None:
            raise ValueError("CASE_NOT_FOUND")

        base = self.evidence_builder.build(db, case_id)
        raw_allowed = role in _RAW_ROLES
        evidence_ids = {str(item["id"]) for item in base.get("evidences") or []}

        if raw_allowed:
            evidence_view = base.get("evidences") or []
            analyzer_view = base.get("analyzers") or {}
            devices = base.get("devices") or []
        else:
            evidence_view = [
                {
                    "id": item.get("id"),
                    "type": item.get("type"),
                    "kind": item.get("kind"),
                    "scope": item.get("scope"),
                    "level": item.get("level"),
                    "completeness": item.get("completeness"),
                }
                for item in (base.get("evidences") or [])
            ]
            analyzer_view = {
                name: {
                    "run_id": item.get("run_id"),
                    "status": item.get("status"),
                    "version": item.get("version"),
                    "summary": item.get("summary") or {},
                    "input_evidence_ids": [
                        x for x in (item.get("input_evidence_ids") or []) if str(x) in evidence_ids
                    ],
                }
                for name, item in (base.get("analyzers") or {}).items()
            }
            devices = [
                {
                    "id": item.get("id"),
                    "platform_id": item.get("platform_id"),
                    "device_info": {
                        key: value
                        for key, value in (item.get("device_info") or {}).items()
                        if key in {"product", "model", "version", "software_version", "firmware_version"}
                    },
                }
                for item in (base.get("devices") or [])
            ]

        report = db.scalar(
            select(PreliminaryEvidenceReport)
            .where(
                PreliminaryEvidenceReport.case_id == case_id,
                PreliminaryEvidenceReport.scope_type == "CASE",
            )
            .order_by(PreliminaryEvidenceReport.version.desc())
            .limit(1)
        )
        findings = []
        if report is not None:
            rows = list(db.scalars(
                select(EvidenceFinding)
                .where(
                    EvidenceFinding.case_id == case_id,
                    EvidenceFinding.scope_type == "CASE",
                    EvidenceFinding.scope_id == case_id,
                )
                .order_by(EvidenceFinding.severity.desc(), EvidenceFinding.updated_at.desc())
            ))
            findings = [
                {
                    "id": row.id,
                    "stable_key": row.stable_key,
                    "finding_type": row.finding_type,
                    "status": row.status,
                    "severity": row.severity,
                    "evidence_level": row.evidence_level,
                    "title": row.title,
                    "observation": row.observation,
                    "interpretation": row.interpretation,
                    "root_cause_boundary": row.root_cause_boundary,
                    "evidence_refs": [
                        ref for ref in (row.evidence_refs_json or [])
                        if str(ref if isinstance(ref, str) else ref.get("evidence_id")) in evidence_ids
                    ],
                }
                for row in rows[:64]
            ]

        diagnosis = db.scalar(
            select(DiagnosisRun)
            .where(DiagnosisRun.case_id == case_id)
            .order_by(DiagnosisRun.created_at.desc())
            .limit(1)
        )
        reproductions = list(db.scalars(
            select(ReproductionSession)
            .where(ReproductionSession.case_id == case_id)
            .order_by(ReproductionSession.created_at.desc())
            .limit(20)
        ))
        experiments = list(db.scalars(
            select(DiagnosticExperiment)
            .where(DiagnosticExperiment.case_id == case_id)
            .order_by(DiagnosticExperiment.created_at.desc())
            .limit(20)
        ))
        fixes = list(db.scalars(
            select(FixVerificationRun)
            .where(FixVerificationRun.case_id == case_id)
            .order_by(FixVerificationRun.created_at.desc())
            .limit(20)
        ))

        out = {
            "schema_version": self.schema_version,
            "case": {
                "id": case.id,
                "case_no": case.case_no,
                "summary": case.summary,
                "status": case.status,
            },
            "viewer_role": role.value,
            "raw_evidence_visible": raw_allowed,
            "devices": devices,
            "evidences": evidence_view,
            "analyzers": analyzer_view,
            "preliminary_report": None if report is None else {
                "id": report.id,
                "version": report.version,
                "status": report.status,
                "completeness": report.completeness_json or {},
                "boundary": report.boundary_json or {},
                "environment": report.environment_json or {},
                "findings": findings,
            },
            "diagnosis": None if diagnosis is None else {
                "id": diagnosis.id,
                "status": diagnosis.status,
                "cycle": diagnosis.cycle,
                "summary": diagnosis.summary_json or {},
                "decision": diagnosis.decision_json or {},
                "updated_at": _dt(_safe_attr(diagnosis, "updated_at")),
            },
            "reproductions": [
                {
                    "id": row.id,
                    "state": row.state,
                    "capture_stage": row.capture_stage,
                    "capture_completeness": row.capture_completeness,
                    "evidence_sufficiency": row.evidence_sufficiency,
                    "cleanup_status": row.cleanup_status,
                    "terminal_reason": row.terminal_reason,
                    "created_at": _dt(row.created_at),
                    "updated_at": _dt(row.updated_at),
                }
                for row in reproductions
            ],
            "experiments": [
                {
                    "id": row.id,
                    "profile_id": _safe_attr(row, "profile_id"),
                    "status": _safe_attr(row, "status"),
                    "causal_state": _safe_attr(row, "causal_state"),
                    "target_finding": _safe_attr(row, "target_finding"),
                    "created_at": _dt(_safe_attr(row, "created_at")),
                    "updated_at": _dt(_safe_attr(row, "updated_at")),
                }
                for row in experiments
            ],
            "fix_verifications": [
                {
                    "id": row.id,
                    "status": _safe_attr(row, "status"),
                    "target_finding": _safe_attr(row, "target_finding"),
                    "result": _safe_attr(row, "result_json", {}) or {},
                    "created_at": _dt(_safe_attr(row, "created_at")),
                    "updated_at": _dt(_safe_attr(row, "updated_at")),
                }
                for row in fixes
            ],
            "authority": {
                "root_cause_status": case.status,
                "ai_can_confirm_root_cause": False,
                "execution_authority": "DETERMINISTIC_ROUTER_RBAC_POLICY_ORCHESTRATOR",
            },
            "fingerprint": base["fingerprint"],
        }
        return out

    @staticmethod
    def allowed_evidence_ids(snapshot: dict[str, Any]) -> set[str]:
        return {str(item.get("id")) for item in snapshot.get("evidences") or [] if item.get("id")}
