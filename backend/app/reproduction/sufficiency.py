from __future__ import annotations

from dataclasses import dataclass

from app.contracts.enums import CaptureChannel, EvidenceGapAction, EvidenceSufficiency
from app.reproduction.profile import ReproductionProfileDefinition


@dataclass(frozen=True)
class SufficiencyDecision:
    status: EvidenceSufficiency
    next_action: EvidenceGapAction
    sufficient: bool
    missing_channels: tuple[str, ...] = ()
    missing_findings: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()


class EvidenceSufficiencyEvaluator:
    """Deterministic gate for one DiagnosticQuestion.

    The evaluator never uses LLM output. It consumes normalized capture/finding facts.
    """

    @staticmethod
    def evaluate(
        profile: ReproductionProfileDefinition,
        *,
        channel_complete: dict[str, bool],
        findings: set[str],
        target_match: bool,
        control_present: bool = False,
        hard_contradiction: bool = False,
        capture_recovery_required: bool = False,
        external_action_required: bool = False,
        enhancement_available: bool = False,
    ) -> SufficiencyDecision:
        req=profile.sufficiency
        missing_channels=tuple(sorted(ch.value for ch in req.must_channels if not channel_complete.get(ch.value,False)))
        missing_findings=tuple(sorted(x for x in req.must_findings if x not in findings))
        reasons=[]
        if req.require_target_match and not target_match:
            reasons.append('TARGET_MATCH_REQUIRED')
        if req.require_control_target_pair and not control_present:
            reasons.append('CONTROL_TARGET_PAIR_REQUIRED')
        if req.require_no_hard_contradiction and hard_contradiction:
            reasons.append('HARD_CONTRADICTION')
        if missing_channels:
            reasons.append('MUST_CAPTURE_INCOMPLETE')
        if missing_findings:
            reasons.append('MUST_FINDING_MISSING')

        if not reasons:
            return SufficiencyDecision(EvidenceSufficiency.SUFFICIENT,EvidenceGapAction.NONE,True)
        if external_action_required:
            return SufficiencyDecision(
                EvidenceSufficiency.INSUFFICIENT_EXTERNAL_ACTION,EvidenceGapAction.EXTERNAL_ACTION_REQUIRED,False,
                missing_channels,missing_findings,tuple(reasons),
            )
        if capture_recovery_required or missing_channels:
            return SufficiencyDecision(
                EvidenceSufficiency.INSUFFICIENT_CAPTURE_RECOVERY,EvidenceGapAction.CAPTURE_RECOVERY,False,
                missing_channels,missing_findings,tuple(reasons),
            )
        # If the target fault has not reproduced, keep the same capture first; enhancement is only useful
        # after a target/anomaly exists but the current evidence depth is insufficient.
        if enhancement_available and target_match and (missing_findings or 'HARD_CONTRADICTION' in reasons):
            return SufficiencyDecision(
                EvidenceSufficiency.INSUFFICIENT_ENHANCE,EvidenceGapAction.ENHANCE_CAPTURE,False,
                missing_channels,missing_findings,tuple(reasons),
            )
        return SufficiencyDecision(
            EvidenceSufficiency.INSUFFICIENT_RETRY,EvidenceGapAction.RETRY_SAME_CAPTURE,False,
            missing_channels,missing_findings,tuple(reasons),
        )
