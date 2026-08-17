from __future__ import annotations

from typing import Iterable, Literal

from pydantic import BaseModel, ConfigDict, Field


ClaimType = Literal["FACT", "BOUNDARY", "CAUSE", "EXCLUSION", "OBSERVATION"]
ClaimStatus = Literal["PROPOSED", "SUPPORTED", "CONTRADICTED", "INSUFFICIENT"]
EvidenceRelation = Literal["SUPPORT", "CONTRADICT"]
EvidenceLevel = Literal["L1", "L2", "L3", "L4", "L5"]
Direction = Literal["RX", "TX", "BIDIRECTIONAL", "UNKNOWN"]


class ClaimEvidenceRef(BaseModel):
    """A scope-aware edge from one diagnostic claim to one Evidence object.

    The edge deliberately carries call/direction/time scope.  An Evidence ID being
    present in the Case is necessary but not sufficient to support a claim; the
    scope must also be explicit enough for a deterministic validator/analyzer to
    reason about it later.
    """

    model_config = ConfigDict(extra="forbid")

    evidence_id: str = Field(min_length=1, max_length=128)
    relation: EvidenceRelation
    call_id: str | None = Field(default=None, max_length=256)
    direction: Direction = "UNKNOWN"
    time_start_ms: int | None = Field(default=None, ge=0)
    time_end_ms: int | None = Field(default=None, ge=0)
    note: str = Field(default="", max_length=1000)


class DiagnosticClaim(BaseModel):
    """Machine-checkable diagnostic statement proposed above analyzer facts."""

    model_config = ConfigDict(extra="forbid")

    claim_id: str = Field(min_length=1, max_length=128)
    claim_type: ClaimType
    statement: str = Field(min_length=1, max_length=4000)
    subject: str = Field(min_length=1, max_length=256)
    predicate: str = Field(min_length=1, max_length=256)
    value: str | int | float | bool | None = None
    status: ClaimStatus = "PROPOSED"
    evidence_level: EvidenceLevel = "L5"
    evidence: list[ClaimEvidenceRef] = Field(default_factory=list, max_length=100)
    missing_evidence: list[str] = Field(default_factory=list, max_length=100)


class ClaimGroundingReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["PASS", "REJECT", "REVIEW"]
    errors: list[dict] = Field(default_factory=list)
    warnings: list[dict] = Field(default_factory=list)
    claim_count: int = 0
    grounded_claim_count: int = 0
    unsupported_claim_count: int = 0


class ClaimGroundingValidator:
    """Fail-closed structural grounding for AI-proposed claims.

    This validator intentionally does *not* decide whether the referenced Evidence
    semantically proves a claim.  It establishes the invariants needed before a
    deterministic Claim/Evidence judge can perform that stronger check:

    * every Evidence reference belongs to the current Case;
    * an AI-created claim remains L5 and cannot self-promote to SUPPORTED;
    * contradictory duplicate edges are rejected;
    * invalid time scopes are rejected;
    * claims without supporting evidence are surfaced for review.
    """

    def validate(
        self,
        claims: Iterable[DiagnosticClaim | dict],
        *,
        allowed_evidence_ids: set[str],
        ai_generated: bool = True,
    ) -> ClaimGroundingReport:
        errors: list[dict] = []
        warnings: list[dict] = []
        normalized = [
            item if isinstance(item, DiagnosticClaim) else DiagnosticClaim.model_validate(item)
            for item in claims
        ]
        grounded = 0
        unsupported = 0

        for claim in normalized:
            if ai_generated and claim.evidence_level != "L5":
                errors.append({
                    "code": "AI_CLAIM_EVIDENCE_LEVEL_INVALID",
                    "claim_id": claim.claim_id,
                    "evidence_level": claim.evidence_level,
                })
            if ai_generated and claim.status in {"SUPPORTED", "CONTRADICTED"}:
                errors.append({
                    "code": "AI_CLAIM_SELF_PROMOTION_FORBIDDEN",
                    "claim_id": claim.claim_id,
                    "status": claim.status,
                })

            support_ids: set[str] = set()
            contradict_ids: set[str] = set()
            for ref in claim.evidence:
                if ref.evidence_id not in allowed_evidence_ids:
                    errors.append({
                        "code": "CLAIM_EVIDENCE_NOT_IN_CASE",
                        "claim_id": claim.claim_id,
                        "evidence_id": ref.evidence_id,
                    })
                if (
                    ref.time_start_ms is not None
                    and ref.time_end_ms is not None
                    and ref.time_end_ms < ref.time_start_ms
                ):
                    errors.append({
                        "code": "CLAIM_EVIDENCE_TIME_SCOPE_INVALID",
                        "claim_id": claim.claim_id,
                        "evidence_id": ref.evidence_id,
                    })
                if ref.relation == "SUPPORT":
                    support_ids.add(ref.evidence_id)
                else:
                    contradict_ids.add(ref.evidence_id)

            for evidence_id in sorted(support_ids & contradict_ids):
                errors.append({
                    "code": "CLAIM_EVIDENCE_RELATION_CONFLICT",
                    "claim_id": claim.claim_id,
                    "evidence_id": evidence_id,
                })

            if support_ids:
                grounded += 1
            else:
                unsupported += 1
                warnings.append({
                    "code": "CLAIM_SUPPORTING_EVIDENCE_MISSING",
                    "claim_id": claim.claim_id,
                })

        status = "REJECT" if errors else ("REVIEW" if warnings else "PASS")
        return ClaimGroundingReport(
            status=status,
            errors=errors,
            warnings=warnings,
            claim_count=len(normalized),
            grounded_claim_count=grounded,
            unsupported_claim_count=unsupported,
        )
