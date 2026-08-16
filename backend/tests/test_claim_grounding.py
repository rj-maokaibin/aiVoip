from app.diagnosis.claim_grounding import ClaimGroundingValidator


def _claim(**overrides):
    value = {
        "claim_id": "C1",
        "claim_type": "BOUNDARY",
        "statement": "PCM RX digits are complete while the upper-layer number buffer misses the first digit.",
        "subject": "dtmf_path",
        "predicate": "mismatch_boundary",
        "value": "PCM_RX_TO_AIM",
        "status": "PROPOSED",
        "evidence_level": "L5",
        "evidence": [
            {"evidence_id": "E_PCM", "relation": "SUPPORT", "direction": "RX"},
            {"evidence_id": "E_LOG", "relation": "SUPPORT", "direction": "RX"},
        ],
    }
    value.update(overrides)
    return value


def test_grounded_ai_claim_passes_structural_gate():
    report = ClaimGroundingValidator().validate(
        [_claim()], allowed_evidence_ids={"E_PCM", "E_LOG"}
    )
    assert report.status == "PASS"
    assert report.grounded_claim_count == 1


def test_ai_claim_cannot_self_promote_or_raise_evidence_level():
    report = ClaimGroundingValidator().validate(
        [_claim(status="SUPPORTED", evidence_level="L2")],
        allowed_evidence_ids={"E_PCM", "E_LOG"},
    )
    assert report.status == "REJECT"
    codes = {row["code"] for row in report.errors}
    assert "AI_CLAIM_SELF_PROMOTION_FORBIDDEN" in codes
    assert "AI_CLAIM_EVIDENCE_LEVEL_INVALID" in codes


def test_cross_case_evidence_and_invalid_scope_are_rejected():
    claim = _claim(evidence=[
        {
            "evidence_id": "E_OTHER_CASE",
            "relation": "SUPPORT",
            "direction": "RX",
            "time_start_ms": 200,
            "time_end_ms": 100,
        }
    ])
    report = ClaimGroundingValidator().validate([claim], allowed_evidence_ids={"E_PCM"})
    assert report.status == "REJECT"
    codes = {row["code"] for row in report.errors}
    assert "CLAIM_EVIDENCE_NOT_IN_CASE" in codes
    assert "CLAIM_EVIDENCE_TIME_SCOPE_INVALID" in codes


def test_claim_without_support_is_review_not_false_pass():
    claim = _claim(evidence=[{"evidence_id": "E_LOG", "relation": "CONTRADICT"}])
    report = ClaimGroundingValidator().validate([claim], allowed_evidence_ids={"E_LOG"})
    assert report.status == "REVIEW"
    assert report.unsupported_claim_count == 1
