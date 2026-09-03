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

    assert visibility["signaling"] == {"caller": "COMPLETE", "callee": "PARTIAL"}
    assert visibility["media"] == {"caller": "BIDIRECTIONAL", "callee": "DOWNSTREAM_ONLY"}
    assert visibility["end_to_end_media"] == "PARTIAL"
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

    assert visibility["media"] == {"caller": "BIDIRECTIONAL", "callee": "BIDIRECTIONAL"}
    assert visibility["end_to_end_media"] == "COMPLETE"
    assert visibility["termination"] == "OBSERVED"
    assert visibility["root_cause_readiness"] == "REVIEWABLE"


def test_no_media_is_unavailable_not_complete():
    visibility = calculate_visibility(media_legs=[])

    assert visibility["media"] == {"caller": "UNAVAILABLE", "callee": "UNAVAILABLE"}
    assert visibility["end_to_end_media"] == "UNAVAILABLE"
