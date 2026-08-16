from __future__ import annotations

import re
import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import AIProposalRecord, Evidence
from app.diagnosis.claim_grounding import ClaimGroundingValidator, DiagnosticClaim
from app.diagnosis.gateway import ReasoningGatewayClient
from app.services.audit import audit


class AIHypothesisProposal(BaseModel):
    model_config = ConfigDict(extra='forbid')

    code: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=512)
    fault_domain: str = Field(min_length=1, max_length=128)
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(min_length=1, max_length=4000)
    supporting_evidence_ids: list[str] = Field(default_factory=list, max_length=100)
    contradicting_evidence_ids: list[str] = Field(default_factory=list, max_length=100)
    missing_evidence: list[str] = Field(default_factory=list, max_length=100)


class AIRecommendedAction(BaseModel):
    model_config = ConfigDict(extra='forbid')

    action_type: Literal[
        'REQUEST_USER_EVIDENCE',
        'RECOMMEND_QUESTION',
        'RECOMMEND_REPRODUCTION_PROFILE',
        'RECOMMEND_EXPERIMENT_PROFILE',
    ]
    reason: str = Field(min_length=1, max_length=2000)
    question_key: str | None = None
    profile_id: str | None = None
    experiment_profile_id: str | None = None


class AIProposal(BaseModel):
    """Non-executable model output contract.

    ``ai-proposal-v1`` remains accepted for compatibility.  ``ai-proposal-v2`` adds
    structured DiagnosticClaim objects but does not increase model authority.
    """

    model_config = ConfigDict(extra='forbid')

    schema_version: Literal['ai-proposal-v1', 'ai-proposal-v2']
    intent: Literal['DIAGNOSIS_ENHANCEMENT']
    hypotheses: list[AIHypothesisProposal] = Field(default_factory=list, max_length=20)
    claims: list[DiagnosticClaim] = Field(default_factory=list, max_length=100)
    known: list[str] = Field(default_factory=list, max_length=100)
    unknown: list[str] = Field(default_factory=list, max_length=100)
    excluded: list[str] = Field(default_factory=list, max_length=100)
    next_question_key: str | None = None
    recommended_action: AIRecommendedAction | None = None
    user_explanation: str = Field(default='', max_length=8000)


_FORBIDDEN_COMMAND = re.compile(
    r'(?:AIM>|\$\(|`[^`]+`|&&|\|\||\bssh\s+\S+@|;\s*(?:rm|ssh|reboot|kill|tcpdump|iptables)\b|'
    r'^\s*(?:rm|ssh|reboot|killall|tcpdump|iptables|systemctl)\s+)',
    re.IGNORECASE | re.MULTILINE,
)


class AIProposalValidator:
    """Validate model output without turning it into executable state.

    Validation is fail-closed. Every AI-created hypothesis is OPEN,
    non-confirmable and L5, with confidence capped at 0.75. AI-created claims are
    likewise L5/PROPOSED and may only reference Evidence owned by the current Case.
    """

    def validate(self, db: Session, *, case_id: str, raw: dict,
                 deterministic_baseline: dict) -> tuple[dict | None, list[dict]]:
        errors: list[dict] = []
        try:
            proposal = AIProposal.model_validate(raw)
        except ValidationError as exc:
            return None, [
                {'code': 'SCHEMA_INVALID', 'path': '.'.join(str(x) for x in item['loc']),
                 'message': item['msg']}
                for item in exc.errors(include_url=False)
            ]

        serialized = proposal.model_dump(mode='json')
        if _FORBIDDEN_COMMAND.search(str(serialized)):
            errors.append({'code': 'COMMAND_OR_TEMPLATE_FORBIDDEN'})

        hypothesis_refs = {
            evidence_id
            for hypothesis in proposal.hypotheses
            for evidence_id in (
                hypothesis.supporting_evidence_ids + hypothesis.contradicting_evidence_ids
            )
        }
        claim_refs = {
            ref.evidence_id
            for claim in proposal.claims
            for ref in claim.evidence
        }
        referenced = hypothesis_refs | claim_refs
        owned: set[str] = set()
        if referenced:
            owned = set(db.scalars(select(Evidence.id).where(
                Evidence.case_id == case_id, Evidence.id.in_(referenced)
            )))
            for evidence_id in sorted(referenced - owned):
                errors.append({'code': 'EVIDENCE_NOT_IN_CASE', 'evidence_id': evidence_id})

        if proposal.claims:
            grounding = ClaimGroundingValidator().validate(
                proposal.claims,
                allowed_evidence_ids=owned,
                ai_generated=True,
            )
            errors.extend(grounding.errors)

        baseline_excluded = set(deterministic_baseline.get('excluded') or [])
        for claim in sorted(set(proposal.known) & baseline_excluded):
            errors.append({'code': 'DETERMINISTIC_FACT_CONFLICT', 'claim': claim})

        question_key = proposal.next_question_key
        action = proposal.recommended_action
        if action and action.action_type == 'RECOMMEND_QUESTION':
            question_key = action.question_key or question_key
        if question_key:
            try:
                from app.reproduction.question_graph import DiagnosticQuestionRegistry
                DiagnosticQuestionRegistry().get(question_key)
            except Exception:
                errors.append({'code': 'QUESTION_NOT_REGISTERED', 'question_key': question_key})

        if action and action.action_type == 'RECOMMEND_REPRODUCTION_PROFILE':
            try:
                from app.reproduction.profile import ReproductionProfileRegistry
                ReproductionProfileRegistry().get(action.profile_id or '')
            except Exception:
                errors.append({'code': 'REPRODUCTION_PROFILE_NOT_REGISTERED',
                               'profile_id': action.profile_id})
        if action and action.action_type == 'RECOMMEND_EXPERIMENT_PROFILE':
            try:
                from app.experiments.profile import ExperimentProfileRegistry
                ExperimentProfileRegistry().get(action.experiment_profile_id or '')
            except Exception:
                errors.append({'code': 'EXPERIMENT_PROFILE_NOT_REGISTERED',
                               'experiment_profile_id': action.experiment_profile_id})

        if errors:
            return None, errors

        for hypothesis in serialized['hypotheses']:
            hypothesis['confidence'] = min(0.75, hypothesis['confidence'])
            hypothesis['status'] = 'OPEN'
            hypothesis['confirmable'] = False
            hypothesis['evidence_level'] = 'L5'
        for claim in serialized['claims']:
            claim['status'] = 'PROPOSED'
            claim['evidence_level'] = 'L5'
        return serialized, []


def _proposal_diff(baseline: dict, proposal: dict | None) -> dict:
    baseline_codes = {str(x.get('code')) for x in baseline.get('hypotheses') or []}
    proposal_codes = {str(x.get('code')) for x in (proposal or {}).get('hypotheses') or []}
    return {
        'baseline_hypothesis_count': len(baseline_codes),
        'proposal_hypothesis_count': len(proposal_codes),
        'overlap_codes': sorted(baseline_codes & proposal_codes),
        'ai_only_codes': sorted(proposal_codes - baseline_codes),
        'baseline_only_codes': sorted(baseline_codes - proposal_codes),
        'formal_result_changed': False,
    }


def run_ai_shadow(db: Session, *, case_id: str, diagnosis_run_id: str | None,
                  snapshot: dict, deterministic_baseline: dict,
                  gateway: ReasoningGatewayClient | None = None) -> AIProposalRecord:
    """Run one best-effort Shadow evaluation and persist all outcomes.

    This function never merges the proposal into ``deterministic_baseline`` and
    never dispatches an action. Gateway/validation failures are data for Eval,
    not diagnosis failures.
    """
    gateway = gateway or ReasoningGatewayClient()
    started = time.monotonic()
    raw: dict | None = None
    validated: dict | None = None
    errors: list[dict] = []
    gateway_error: str | None = None
    status = 'DEGRADED'
    try:
        if not gateway.enabled():
            raise RuntimeError('REASONING_GATEWAY_DISABLED')
        response = gateway.enhance(snapshot, deterministic_baseline)
        raw = response.get('proposal') if isinstance(response.get('proposal'), dict) else response
        validated, errors = AIProposalValidator().validate(
            db, case_id=case_id, raw=raw, deterministic_baseline=deterministic_baseline
        )
        status = 'ACCEPTED' if validated is not None else 'REJECTED'
    except Exception as exc:
        gateway_error = f'{type(exc).__name__}:{exc}'
        status = 'DEGRADED'

    latency_ms = int((time.monotonic() - started) * 1000)
    row = AIProposalRecord(
        case_id=case_id,
        diagnosis_run_id=diagnosis_run_id,
        schema_version=(raw or {}).get('schema_version', 'ai-proposal-v1'),
        intent=(raw or {}).get('intent', 'DIAGNOSIS_ENHANCEMENT'),
        mode='SHADOW',
        status=status,
        input_fingerprint=str(snapshot.get('fingerprint') or ''),
        model_name=gateway.model or None,
        prompt_version=settings.reasoning_prompt_version,
        workflow_version=settings.ai_shadow_workflow_version,
        latency_ms=latency_ms,
        raw_output_json=raw,
        validated_output_json=validated,
        validation_errors=errors,
        baseline_json=deterministic_baseline,
        diff_json=_proposal_diff(deterministic_baseline, validated),
        gateway_error=gateway_error,
    )
    db.add(row)
    db.flush()
    audit(
        db, case_id=case_id, actor='ai-shadow', event_type='AI_PROPOSAL_EVALUATED',
        target_type='ai_proposal', target_id=row.id,
        detail={'status': status, 'model': gateway.model, 'latency_ms': latency_ms,
                'validation_error_count': len(errors), 'formal_result_changed': False,
                'claim_count': len((validated or {}).get('claims') or [])},
    )
    return row
