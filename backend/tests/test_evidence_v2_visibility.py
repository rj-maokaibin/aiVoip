from app.reports.v2.visibility import calculate_visibility


def test_partial_callee_media_does_not_become_end_to_end_complete():
    visibility = calculate_visibility(
        signaling_legs=[
            {"role": "CALLER", "observed": ["INVITE", "FINAL_RESPONSE", "ACK"]},
            {"role": "CALLEE", "observed": ["INVITE", "FINAL_RESPONSE"]},
        ],
        media_legs=[
            {"role": "CALLER", "directions": ["UPSTREAM", "DOWNSTREAM"]},
            {"role": "CALLEE", "directions": ["DOWNSTREAM"]},
        ],
        termination={"observed": False},
    )

    assert visibility["acquisition"] == "AVAILABLE"
    assert visibility["signaling"] == {"caller_leg": "COMPLETE", "callee_leg": "PARTIAL"}
    assert visibility["media"]["caller_leg"] == "BIDIRECTIONAL"
    assert visibility["media"]["callee_leg"] == "ONE_WAY"
    assert visibility["media"]["callee_leg_directions"] == ["DOWNSTREAM"]
    assert visibility["media"]["end_to_end"] == "PARTIAL"
    assert visibility["termination"] == "NOT_OBSERVED"
    assert visibility["root_cause_readiness"] == "INSUFFICIENT"


def test_end_to_end_complete_requires_both_media_legs_bidirectional():
    visibility = calculate_visibility(
        media_legs=[
            {"role": "CALLER", "directions": ["UPSTREAM", "DOWNSTREAM"]},
            {"role": "CALLEE", "directions": ["UPSTREAM", "DOWNSTREAM"]},
        ],
        termination={"observed": True},
        required_root_cause_evidence_complete=True,
    )

    assert visibility["media"]["caller_leg"] == "BIDIRECTIONAL"
    assert visibility["media"]["callee_leg"] == "BIDIRECTIONAL"
    assert visibility["media"]["end_to_end"] == "COMPLETE"
    assert visibility["termination"] == "OBSERVED"
    assert visibility["root_cause_readiness"] == "SUFFICIENT"


def test_no_media_is_missing_and_end_to_end_unknown():
    visibility = calculate_visibility(media_legs=[])

    assert visibility["media"]["caller_leg"] == "MISSING"
    assert visibility["media"]["callee_leg"] == "MISSING"
    assert visibility["media"]["end_to_end"] == "UNKNOWN"
