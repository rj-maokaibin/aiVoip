from __future__ import annotations

from types import SimpleNamespace

from tools.m7_acceptance_gate import CRITERIA, _ai_authority_safe, _is_real_session, evaluate_signals


def _all(value: bool = True) -> dict[str, bool]:
    return {key: value for _, key, _, _ in CRITERIA}


def test_m7_all_twenty_criteria_pass_without_root_cause_requirement():
    report = evaluate_signals(
        _all(True),
        observed={"golden": {"status": "PARTIAL_GOLDEN", "verification_tier": None}},
    )
    assert report["schema_version"] == "m7-real-dut-acceptance-v1"
    assert report["status"] == "PASS"
    assert report["criteria_total"] == 20
    assert report["criteria_passed"] == 20
    assert report["blocked_ids"] == []
    assert report["promotion_eligible"] is False
    assert report["root_cause_confirmation_required_for_m7"] is False


def test_m7_reports_precise_missing_closed_loop_capabilities():
    signals = _all(True)
    signals["pcm_tx_present"] = False
    signals["ai_shadow_present"] = False
    signals["cleanup_verified"] = False
    report = evaluate_signals(signals)
    assert report["status"] == "BLOCKED"
    assert report["criteria_passed"] == 17
    assert report["blocked_ids"] == ["M7-06", "M7-11", "M7-16"]
    rows = {row["id"]: row for row in report["criteria"]}
    assert rows["M7-06"]["remediation"]
    assert rows["M7-11"]["remediation"]
    assert rows["M7-16"]["remediation"]


def test_ai_authority_safety_accepts_only_l5_nonconfirmable_proposals():
    safe = SimpleNamespace(
        mode="SHADOW",
        status="ACCEPTED",
        diff_json={"formal_result_changed": False},
        validated_output_json={
            "hypotheses": [
                {"code": "AI_CANDIDATE", "status": "OPEN", "confirmable": False, "evidence_level": "L5"}
            ],
            "claims": [{"status": "PROPOSED", "evidence_level": "L5"}],
        },
    )
    assert _ai_authority_safe([safe]) is True


def test_ai_authority_safety_blocks_formal_change_or_confirmed_ai_claim():
    changed = SimpleNamespace(
        mode="SHADOW",
        status="ACCEPTED",
        diff_json={"formal_result_changed": True},
        validated_output_json={"hypotheses": [], "claims": []},
    )
    confirmed = SimpleNamespace(
        mode="SHADOW",
        status="ACCEPTED",
        diff_json={"formal_result_changed": False},
        validated_output_json={
            "hypotheses": [
                {"status": "CONFIRMED", "confirmable": True, "evidence_level": "L1"}
            ],
            "claims": [],
        },
    )
    assert _ai_authority_safe([changed]) is False
    assert _ai_authority_safe([confirmed]) is False


def test_rejected_or_degraded_ai_rows_do_not_create_false_m7_ai_pass():
    rejected = SimpleNamespace(
        mode="SHADOW",
        status="REJECTED",
        diff_json={"formal_result_changed": False},
        validated_output_json=None,
    )
    degraded = SimpleNamespace(
        mode="SHADOW",
        status="DEGRADED",
        diff_json={"formal_result_changed": False},
        validated_output_json=None,
    )
    assert _ai_authority_safe([rejected, degraded]) is False


def test_m7_real_platform_guard_rejects_mock_or_missing_platform_profile():
    assert _is_real_session(SimpleNamespace(platform_profile_id="ruijie-voip-aim-real")) is True
    assert _is_real_session(SimpleNamespace(platform_profile_id="ruijie-voip-mock")) is False
    assert _is_real_session(SimpleNamespace(platform_profile_id=None)) is False
