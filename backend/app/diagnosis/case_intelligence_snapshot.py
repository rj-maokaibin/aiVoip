from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.contracts.enums import UserRole
from app.db.ai_intelligence_models import AICaseCopilotRecord, AISemanticIntentRecord
from app.db.evidence_report_models import EvidenceFinding, PreliminaryEvidenceReport
from app.db.models import (
    AuditLog,
    Case,
    DiagnosisRun,
    DiagnosticExperiment,
    FeishuCaseBinding,
    FixVerificationRun,
    Hypothesis,
    HypothesisEvidence,
    ReproductionCall,
    ReproductionSession,
)
from app.diagnosis.snapshot import CaseEvidenceSnapshotBuilder


_RAW_ROLES = {UserRole.ENGINEER, UserRole.EXPERT_REVIEWER, UserRole.ADMIN, UserRole.SERVICE}


def _dt(value):
    return value.isoformat() if value is not None else None


def _fingerprint(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class CaseIntelligenceSnapshotBuilder:
    """Canonical shared Case Intelligence Snapshot V1 builder.

    AI1/AI2/AI3 must consume projections from this builder rather than assembling
    independent Case context. The canonical snapshot remains current-Case scoped;
    cross-Case similarities/knowledge are reserved reference-context fields and are
    intentionally empty in the base/Copilot projection unless a later governed
    reasoning flow explicitly enriches them.
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
        base_evidences = list(base.get("evidences") or [])
        visible_source = base_evidences if raw_allowed else [
            item for item in base_evidences if str(item.get("kind") or "").upper() != "RAW"
        ]
        evidence_ids = {str(item["id"]) for item in visible_source if item.get("id")}

        if raw_allowed:
            evidence_view = visible_source
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
                for item in visible_source
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
        findings: list[dict[str, Any]] = []
        if report is not None:
            rows = list(db.scalars(
                select(EvidenceFinding)
                .where(
                    EvidenceFinding.case_id == case_id,
                    EvidenceFinding.scope_type == "CASE",
                    EvidenceFinding.scope_id == case_id,
                )
                .order_by(EvidenceFinding.severity.desc(), EvidenceFinding.updated_at.desc())
                .limit(64)
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
                for row in rows
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
        calls = list(db.scalars(
            select(ReproductionCall)
            .where(ReproductionCall.case_id == case_id)
            .order_by(ReproductionCall.started_at.desc())
            .limit(50)
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
        hypotheses = list(db.scalars(
            select(Hypothesis)
            .where(Hypothesis.case_id == case_id)
            .order_by(Hypothesis.updated_at.desc())
            .limit(50)
        ))
        hypothesis_ids = [row.id for row in hypotheses]
        hypothesis_evidence: dict[str, list[dict[str, Any]]] = {row.id: [] for row in hypotheses}
        if hypothesis_ids:
            for link in db.scalars(
                select(HypothesisEvidence)
                .where(HypothesisEvidence.hypothesis_id.in_(hypothesis_ids))
                .order_by(HypothesisEvidence.created_at.asc())
            ):
                if link.ref_type.upper() == "EVIDENCE" and str(link.ref_id) not in evidence_ids:
                    continue
                hypothesis_evidence.setdefault(link.hypothesis_id, []).append({
                    "ref_type": link.ref_type,
                    "ref_id": link.ref_id,
                    "evidence_level": link.evidence_level,
                    "direction": link.direction,
                    "weight": link.weight,
                })

        binding = db.scalar(
            select(FeishuCaseBinding).where(FeishuCaseBinding.case_id == case_id).limit(1)
        )
        semantic_records = list(db.scalars(
            select(AISemanticIntentRecord)
            .where(AISemanticIntentRecord.case_id == case_id)
            .order_by(AISemanticIntentRecord.created_at.desc())
            .limit(20)
        ))
        copilot_records = list(db.scalars(
            select(AICaseCopilotRecord)
            .where(AICaseCopilotRecord.case_id == case_id)
            .order_by(AICaseCopilotRecord.created_at.desc())
            .limit(20)
        ))
        audits = list(db.scalars(
            select(AuditLog)
            .where(AuditLog.case_id == case_id)
            .order_by(AuditLog.created_at.desc())
            .limit(30)
        ))

        snapshot: dict[str, Any] = {
            "schema_version": self.schema_version,
            "case": {
                "id": case.id,
                "case_no": case.case_no,
                "summary": case.summary,
                "status": case.status,
                "created_at": _dt(case.created_at),
                "updated_at": _dt(case.updated_at),
            },
            "conversation": {
                "source_chat_bound": binding is not None,
                "chat_type": binding.source_chat_type if binding else None,
                "tenant_bound": bool(binding and binding.source_tenant_key),
                "source_message_present": bool(binding and binding.source_message_id),
                "raw_text_in_snapshot": False,
            },
            "identities": {
                "requester_role": role.value,
                "raw_evidence_visible": raw_allowed,
            },
            "devices": devices,
            "reproductions": [
                {
                    "id": row.id,
                    "state": row.state,
                    "profile_key": row.profile_key,
                    "profile_version": row.profile_version,
                    "capture_stage": row.capture_stage,
                    "capture_completeness": row.capture_completeness,
                    "evidence_sufficiency": row.evidence_sufficiency,
                    "cleanup_status": row.cleanup_status,
                    "primary_target_call_id": row.primary_target_call_id,
                    "terminal_reason": row.terminal_reason,
                    "started_at": _dt(row.started_at),
                    "ended_at": _dt(row.ended_at),
                    "created_at": _dt(row.created_at),
                    "updated_at": _dt(row.updated_at),
                }
                for row in reproductions
            ],
            "calls": [
                {
                    "id": row.id,
                    "session_id": row.session_id,
                    "call_no": row.call_no,
                    "status": row.status,
                    "verdict": row.verdict,
                    "role": row.role,
                    "live_summary": row.live_summary_json or {},
                    "quick_analysis": row.quick_analysis_json or {},
                    "started_at": _dt(row.started_at),
                    "ended_at": _dt(row.ended_at),
                }
                for row in calls
            ],
            "evidences": evidence_view,
            "analyzers": analyzer_view,
            "preliminary_reports": [] if report is None else [{
                "id": report.id,
                "version": report.version,
                "status": report.status,
                "completeness": report.completeness_json or {},
                "boundary": report.boundary_json or {},
                "environment": report.environment_json or {},
                "findings": findings,
            }],
            "diagnoses": [] if diagnosis is None else [{
                "id": diagnosis.id,
                "status": diagnosis.status,
                "cycle": diagnosis.cycle,
                "summary": diagnosis.summary_json or {},
                "decision": diagnosis.decision_json or {},
                "created_at": _dt(diagnosis.created_at),
                "updated_at": _dt(diagnosis.updated_at),
            }],
            "hypotheses": [
                {
                    "id": row.id,
                    "code": row.code,
                    "title": row.title,
                    "fault_domain": row.fault_domain,
                    "status": row.status,
                    "confidence": row.confidence,
                    "rationale": row.rationale,
                    "confirmable": row.confirmable,
                    "confirm_rule": row.confirm_rule,
                    "evidence": hypothesis_evidence.get(row.id, []),
                    "created_at": _dt(row.created_at),
                    "updated_at": _dt(row.updated_at),
                }
                for row in hypotheses
            ],
            "experiments": [
                {
                    "id": row.id,
                    "profile_key": row.profile_key,
                    "profile_version": row.profile_version,
                    "state": row.state,
                    "confirmation_policy": row.confirmation_policy,
                    "independent_variable": row.independent_variable,
                    "causal_state": row.causal_state,
                    "target_finding": row.target_finding,
                    "current_round": row.current_round,
                    "terminal_reason": row.terminal_reason,
                    "created_at": _dt(row.created_at),
                    "updated_at": _dt(row.updated_at),
                }
                for row in experiments
            ],
            "fix_verifications": [
                {
                    "id": row.id,
                    "fix_action_id": row.fix_action_id,
                    "status": row.status,
                    "target_finding": row.target_finding,
                    "verification_call_count": row.verification_call_count,
                    "successful_call_count": row.successful_call_count,
                    "environment_status": row.environment_status,
                    "evaluations": row.evaluations_json or [],
                    "business_checks": row.business_checks_json or {},
                    "comparison": row.comparison_json or {},
                    "evidence_id": row.evidence_id if raw_allowed or str(row.evidence_id or "") in evidence_ids else None,
                    "created_at": _dt(row.created_at),
                    "updated_at": _dt(row.updated_at),
                }
                for row in fixes
            ],
            # Current-Case base snapshot deliberately does not mix historical
            # similarity or knowledge into evidence authority. AI2 may enrich
            # these fields as clearly marked Reference Context later.
            "similar_cases": [],
            "knowledge": [],
            "recent_interactions": [
                *[
                    {
                        "type": "AI1_SEMANTIC",
                        "id": row.id,
                        "status": row.status,
                        "deterministic_intent": row.deterministic_intent,
                        "validated_intent": row.validated_intent,
                        "created_at": _dt(row.created_at),
                    }
                    for row in semantic_records
                ],
                *[
                    {
                        "type": "AI3_COPILOT",
                        "id": row.id,
                        "status": row.status,
                        "routed_control_intent": row.routed_control_intent,
                        "created_at": _dt(row.created_at),
                    }
                    for row in copilot_records
                ],
            ][:40],
            "audit_digest": [
                {
                    "event_type": row.event_type,
                    "actor_type": row.actor_type,
                    "target_type": row.target_type,
                    "created_at": _dt(row.created_at),
                }
                for row in audits
            ],
            "authority": {
                "root_cause_status": case.status,
                "ai_can_confirm_root_cause": False,
                "execution_authority": "DETERMINISTIC_ROUTER_RBAC_POLICY_ORCHESTRATOR",
            },
            "source_evidence_fingerprint": base.get("fingerprint"),
        }
        snapshot["snapshot_fingerprint"] = _fingerprint(snapshot)
        # Compatibility alias for existing AI3 records/callers.
        snapshot["fingerprint"] = snapshot["snapshot_fingerprint"]
        return snapshot

    def build_for_copilot(self, db: Session, case_id: str, *, role: UserRole) -> dict[str, Any]:
        return self.build(db, case_id, role=role)

    def build_for_reasoning(self, db: Session, case_id: str) -> dict[str, Any]:
        """Engineering projection reserved for AI2; execution authority unchanged."""
        return self.build(db, case_id, role=UserRole.SERVICE)

    def build_for_semantic_router(self, db: Session, case_id: str) -> dict[str, Any]:
        """Minimal non-raw projection for AI1 contextual semantics."""
        full = self.build(db, case_id, role=UserRole.VIEWER)
        reports = full.get("preliminary_reports") or []
        diagnoses = full.get("diagnoses") or []
        return {
            "schema_version": full["schema_version"],
            "case": full["case"],
            "conversation": full["conversation"],
            "devices": full["devices"],
            "reproductions": (full.get("reproductions") or [])[:5],
            "calls": (full.get("calls") or [])[:10],
            "preliminary_reports": reports[:1],
            "diagnoses": diagnoses[:1],
            "hypotheses": (full.get("hypotheses") or [])[:10],
            "snapshot_fingerprint": full["snapshot_fingerprint"],
            "authority": full["authority"],
        }

    @staticmethod
    def allowed_evidence_ids(snapshot: dict[str, Any]) -> set[str]:
        return {str(item.get("id")) for item in snapshot.get("evidences") or [] if item.get("id")}
