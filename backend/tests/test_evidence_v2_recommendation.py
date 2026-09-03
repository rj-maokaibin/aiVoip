from app.reports.v2.recommendation import generate_recommendations


def test_timing_cluster_generates_bound_executable_recommendation():
    recommendations = generate_recommendations(
        clusters=[{"cluster_id": "CC-1", "type": "CROSS_LAYER_MEDIA_TIMING_SPIKE"}],
        visibility={"end_to_end_media": "PARTIAL"},
    )

    timing = recommendations[0]
    assert timing["cluster_refs"] == ["CC-1"]
    assert timing["priority"] == "P0"
    assert timing["decision_rule"]
    assert timing["pass_criteria"]
    assert "HIGH" not in timing["action"]


def test_normal_finding_does_not_generate_problem_action():
    recommendations = generate_recommendations(
        findings=[{"finding_id": "N1", "class": "NORMAL", "type": "DTMF_SIP_DIAL_MATCH"}],
        visibility={"end_to_end_media": "COMPLETE"},
    )
    assert recommendations == []
