from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from app.diagnosis.claim_grounding import ClaimEvidenceRef, DiagnosticClaim


@dataclass(frozen=True)
class PathObservation:
    stage: str
    value: Any
    evidence_id: str
    call_id: str | None = None
    direction: str = "UNKNOWN"


def derive_first_mismatch_boundary(
    *,
    path_name: str,
    reference_value: Any,
    observations: Iterable[PathObservation],
) -> DiagnosticClaim | None:
    """Create an L5 boundary claim at the first path transition that changes value.

    This helper does not confirm the claim. It converts aligned deterministic
    observations into a machine-checkable boundary proposal that still requires the
    normal Claim/Evidence judge before any formal diagnosis state can advance.
    """
    rows = list(observations)
    if not rows:
        return None
    previous_value = reference_value
    previous_stage = "REFERENCE"
    previous_evidence = None
    for row in rows:
        if row.value != previous_value:
            evidence = []
            if previous_evidence:
                evidence.append(ClaimEvidenceRef(
                    evidence_id=previous_evidence,
                    relation="SUPPORT",
                    call_id=row.call_id,
                    direction=row.direction if row.direction in {"RX", "TX", "BIDIRECTIONAL", "UNKNOWN"} else "UNKNOWN",
                    note=f"{previous_stage} preserved upstream value",
                ))
            evidence.append(ClaimEvidenceRef(
                evidence_id=row.evidence_id,
                relation="SUPPORT",
                call_id=row.call_id,
                direction=row.direction if row.direction in {"RX", "TX", "BIDIRECTIONAL", "UNKNOWN"} else "UNKNOWN",
                note=f"first changed value observed at {row.stage}",
            ))
            return DiagnosticClaim(
                claim_id=f"BOUNDARY:{path_name}:{previous_stage}->{row.stage}",
                claim_type="BOUNDARY",
                statement=f"{path_name} first mismatch is between {previous_stage} and {row.stage}",
                subject=path_name,
                predicate="FIRST_MISMATCH_BOUNDARY",
                value=f"{previous_stage}->{row.stage}",
                status="PROPOSED",
                evidence_level="L5",
                evidence=evidence,
            )
        previous_value = row.value
        previous_stage = row.stage
        previous_evidence = row.evidence_id
    return None


def claim_graph(claims: Iterable[DiagnosticClaim | dict]) -> dict:
    normalized = [
        item if isinstance(item, DiagnosticClaim) else DiagnosticClaim.model_validate(item)
        for item in claims
    ]
    return {
        "schema_version": "diagnostic-claim-graph-v1",
        "nodes": [claim.model_dump(mode="json") for claim in normalized],
        "edges": [
            {
                "claim_id": claim.claim_id,
                "evidence_id": edge.evidence_id,
                "relation": edge.relation,
                "call_id": edge.call_id,
                "direction": edge.direction,
                "time_start_ms": edge.time_start_ms,
                "time_end_ms": edge.time_end_ms,
            }
            for claim in normalized
            for edge in claim.evidence
        ],
        "boundary_candidates": [
            claim.claim_id for claim in normalized if claim.claim_type == "BOUNDARY"
        ],
        "formal_authority": False,
    }
