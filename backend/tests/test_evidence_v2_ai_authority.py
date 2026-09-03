import pytest

from app.reports.v2.ai_authority import (
    AIAuthorityViolation,
    ai_explanation_allowed,
    sanitize_ai_report_output,
)


def test_ai_may_add_interpretation_but_hypotheses_stay_candidates():
    output = sanitize_ai_report_output({
        "language_summary": "存在跨层 timing spike。",
        "hypotheses": [{"statement": "可能存在调度抖动", "status": "CONFIRMED"}],
        "next_experiments": ["采集 CPU/softirq"],
    })
    assert output["authority"] == "INTERPRETATION_ONLY"
    assert output["root_cause_confirmed"] is False
    assert output["hypotheses"][0]["status"] == "CANDIDATE"
    assert output["hypotheses"][0]["root_cause_confirmed"] is False


def test_ai_cannot_write_call_end_loss_visibility_or_root_cause():
    for field, value in (
        ("call_end_time", 1.0),
        ("lost_packets", 3),
        ("visibility", {"end_to_end_media": "COMPLETE"}),
        ("root_cause_status", "CONFIRMED"),
    ):
        with pytest.raises(AIAuthorityViolation):
            sanitize_ai_report_output({field: value})


def test_ai_explanation_requires_semantic_pass_and_publishable_report():
    assert ai_explanation_allowed({"semantic_validation": {"status": "PASS"}, "publishable": True}) is True
    assert ai_explanation_allowed({"semantic_validation": {"status": "FAIL"}, "publishable": False}) is False
