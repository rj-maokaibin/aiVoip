from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.contracts.case_copilot import CaseCopilotProposal
from app.contracts.enums import UserRole
from app.db.ai_intelligence_models import AICaseCopilotRecord
from app.diagnosis.claim_grounding import ClaimGroundingValidator
from app.integrations.feishu.intake import route_intake
from app.services.audit import audit
from app.copilot.gateway import CaseCopilotGatewayClient, CopilotGatewayError
from app.copilot.snapshot import CaseIntelligenceSnapshotBuilder


_CONTROL_INTENTS = {"STOP_REPRODUCTION", "EXTERNAL_ACTION_COMPLETED", "FIX_APPLIED"}
_REGISTERED_EXPERIMENT_CONTROL = re.compile(r"(?i)(?:开始|启动|执行|运行).{0,12}(?:实验|experiment)")


@dataclass(frozen=True)
class CopilotResult:
    status: str
    answer: str
    proposal: dict[str, Any] | None
    grounding: dict[str, Any]
    record_id: str
    routed_control_intent: str | None = None
    error_code: str | None = None


def _question_hash(question: str) -> str:
    return hashlib.sha256((question or "").encode("utf-8")).hexdigest()


def _record_to_result(row: AICaseCopilotRecord) -> CopilotResult:
    proposal = row.proposal_json or None
    answer = str((proposal or {}).get("answer") or "")
    if row.status == "CONTROL_INTENT_REQUIRED" and not answer:
        answer = "该请求属于受控操作，Case Copilot 不直接执行；请通过受控 Intent / RBAC / Policy 路径处理。"
    return CopilotResult(
        status=row.status,
        answer=answer,
        proposal=proposal,
        grounding=row.grounding_report_json or {},
        record_id=row.id,
        routed_control_intent=row.routed_control_intent,
        error_code=row.error_code,
    )


def _control_intent(question: str) -> str | None:
    intake = route_intake(text=question, attachments=[], has_thread_case=True)
    if intake.intent in _CONTROL_INTENTS:
        return intake.intent
    if _REGISTERED_EXPERIMENT_CONTROL.search(question or ""):
        return "START_REGISTERED_EXPERIMENT"
    return None


def _claim_evidence_ids(proposal: CaseCopilotProposal) -> set[str]:
    return {str(ref.evidence_id) for claim in proposal.claims for ref in claim.evidence}


class CaseCopilotService:
    """Read-only AI3 orchestration with current-Case Claim Grounding.

    The service has no reproduction/experiment/fix dispatcher. Control requests
    route out before the model. Grounded answers are released only when the
    requester's role has at least one authorized Evidence object and the public
    citation set exactly covers Evidence used by structured Claims.
    """

    def __init__(
        self,
        *,
        snapshot_builder: CaseIntelligenceSnapshotBuilder | None = None,
        gateway: CaseCopilotGatewayClient | None = None,
    ):
        self.snapshot_builder = snapshot_builder or CaseIntelligenceSnapshotBuilder()
        self.gateway = gateway or CaseCopilotGatewayClient()

    def _persist_rejection(
        self,
        db: Session,
        *,
        common: dict[str, Any],
        status: str,
        code: str,
    ) -> CopilotResult:
        row = AICaseCopilotRecord(
            status=status,
            proposal_json={},
            grounding_report_json={"status": "REJECT", "error_code": code},
            routed_control_intent=None,
            model_name=None,
            error_code=code,
            **common,
        )
        db.add(row)
        db.flush()
        audit(
            db,
            case_id=common["case_id"],
            actor=common["actor_id"],
            event_type="AI_CASE_COPILOT_REJECTED",
            target_type="ai_case_copilot",
            target_id=row.id,
            detail={
                "schema_version": "ai-case-copilot-audit-v1",
                "status": status,
                "error_code": code,
                "read_only": True,
            },
        )
        return _record_to_result(row)

    def answer(
        self,
        db: Session,
        *,
        case_id: str,
        question: str,
        request_key: str,
        actor_id: str,
        actor_role: UserRole,
    ) -> CopilotResult:
        existing = db.scalar(
            select(AICaseCopilotRecord)
            .where(AICaseCopilotRecord.request_key == request_key)
            .limit(1)
        )
        if existing is not None:
            if existing.case_id != case_id:
                raise ValueError("COPILOT_REQUEST_KEY_CASE_CONFLICT")
            if existing.actor_id != actor_id:
                raise ValueError("COPILOT_REQUEST_KEY_ACTOR_CONFLICT")
            if existing.actor_role != actor_role.value:
                raise ValueError("COPILOT_REQUEST_KEY_ROLE_CONFLICT")
            return _record_to_result(existing)

        snapshot = self.snapshot_builder.build(db, case_id, role=actor_role)
        control = _control_intent(question)
        common = dict(
            case_id=case_id,
            request_key=request_key,
            actor_id=actor_id,
            actor_role=actor_role.value,
            question_hash=_question_hash(question),
            snapshot_fingerprint=str(snapshot.get("fingerprint") or ""),
            prompt_version="ai-case-copilot-v1",
        )
        if control:
            proposal = {
                "schema_version": "ai-case-copilot-v1-control-route",
                "answer": "该请求属于受控操作，Case Copilot 不直接执行；请通过受控 Intent / RBAC / Policy 路径处理。",
                "requested_control_intent": control,
                "execution_authority": "DETERMINISTIC_ROUTER_RBAC_POLICY_ORCHESTRATOR",
            }
            row = AICaseCopilotRecord(
                status="CONTROL_INTENT_REQUIRED",
                proposal_json=proposal,
                grounding_report_json={"status": "NOT_APPLICABLE", "reason": "CONTROL_REQUEST_ROUTED_OUT"},
                routed_control_intent=control,
                model_name=None,
                error_code=None,
                **common,
            )
            db.add(row)
            db.flush()
            audit(
                db,
                case_id=case_id,
                actor=actor_id,
                event_type="AI_CASE_COPILOT_CONTROL_ROUTED",
                target_type="ai_case_copilot",
                target_id=row.id,
                detail={
                    "schema_version": "ai-case-copilot-audit-v1",
                    "control_intent": control,
                    "executed": False,
                },
            )
            return _record_to_result(row)

        allowed = self.snapshot_builder.allowed_evidence_ids(snapshot)
        if not allowed:
            return self._persist_rejection(
                db,
                common=common,
                status="REJECTED",
                code="COPILOT_NO_AUTHORIZED_EVIDENCE",
            )

        try:
            raw = self.gateway.answer(question=question, snapshot=snapshot)
            proposal = CaseCopilotProposal.model_validate(raw["proposal"])
            grounding = ClaimGroundingValidator().validate(
                proposal.claims,
                allowed_evidence_ids=allowed,
                ai_generated=True,
            )
            cited = {str(x) for x in proposal.cited_evidence_ids}
            claim_refs = _claim_evidence_ids(proposal)
            unknown_citations = sorted(cited - allowed)
            unbound_citations = sorted(cited - claim_refs)
            missing_public_citations = sorted(claim_refs - cited)
            if unknown_citations:
                raise ValueError(f"COPILOT_EVIDENCE_NOT_IN_CASE:{','.join(unknown_citations[:8])}")
            if unbound_citations:
                raise ValueError(f"COPILOT_CITATION_NOT_BOUND_TO_CLAIM:{','.join(unbound_citations[:8])}")
            if missing_public_citations:
                raise ValueError(f"COPILOT_CLAIM_EVIDENCE_NOT_PUBLICLY_CITED:{','.join(missing_public_citations[:8])}")
            if grounding.status != "PASS":
                codes = [str(x.get("code")) for x in grounding.errors + grounding.warnings]
                raise ValueError(f"COPILOT_GROUNDING_NOT_PASS:{','.join(codes[:8])}")
            row = AICaseCopilotRecord(
                status="ANSWERED",
                proposal_json=proposal.model_dump(mode="json"),
                grounding_report_json=grounding.model_dump(mode="json"),
                routed_control_intent=None,
                model_name=str(raw.get("model") or "")[:128] or None,
                prompt_version=str(raw.get("prompt_version") or "ai-case-copilot-v1")[:64],
                error_code=None,
                **{k: v for k, v in common.items() if k != "prompt_version"},
            )
            db.add(row)
            db.flush()
            audit(
                db,
                case_id=case_id,
                actor=actor_id,
                event_type="AI_CASE_COPILOT_ANSWERED",
                target_type="ai_case_copilot",
                target_id=row.id,
                detail={
                    "schema_version": "ai-case-copilot-audit-v1",
                    "claim_count": len(proposal.claims),
                    "cited_evidence_count": len(cited),
                    "grounding_status": grounding.status,
                    "actor_role": actor_role.value,
                    "read_only": True,
                },
            )
            return _record_to_result(row)
        except CopilotGatewayError as exc:
            status = "GATEWAY_FAILED"
            code = str(exc).split(":", 1)[0][:128]
        except (ValidationError, ValueError, KeyError, TypeError) as exc:
            status = "REJECTED"
            code = str(exc).split("\n", 1)[0][:128]

        return self._persist_rejection(db, common=common, status=status, code=code)
