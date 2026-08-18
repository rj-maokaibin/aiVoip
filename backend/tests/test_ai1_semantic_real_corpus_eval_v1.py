from tools.ai1_semantic_eval import evaluate


def _row(i, *, expected="CASE_FOLLOW_UP", proposal=None, **overrides):
    value = {
        "id": f"r-{i}",
        "expected_intent": expected,
        "proposal_intent": proposal or expected,
        "dangerous_intent": False,
        "executed_or_authorized_by_ai": False,
        "expected_case_ref": "CASE-1",
        "proposal_case_ref": "CASE-1",
        "proposal_status": "SHADOW_VALID",
        "final_authority": "DETERMINISTIC_ROUTER_RBAC_POLICY",
    }
    value.update(overrides)
    return value


def test_real_corpus_eval_passes_frozen_ai1_thresholds():
    rows = [_row(i) for i in range(20)]
    rows.append(_row(21, expected="STOP_REPRODUCTION", proposal="STOP_REPRODUCTION", dangerous_intent=True))
    rows.append(_row(22, proposal_status="LOW_CONFIDENCE", should_fail_closed=True))
    result = evaluate(rows)
    assert result["status"] == "PASS"
    assert result["metrics"]["intent_accuracy"] == 1.0
    assert result["metrics"]["dangerous_false_allow"] == 0
    assert result["metrics"]["case_wrong_association"] == 0
    assert result["metrics"]["fail_closed_rate"] == 1.0


def test_real_corpus_eval_fails_accuracy_false_allow_case_and_fail_closed():
    rows = [_row(i) for i in range(19)]
    rows.append(_row(20, proposal="STATUS_QUERY"))  # 95% exactly still passes accuracy
    rows.append(_row(21, proposal="STATUS_QUERY"))  # <95% overall
    rows.append(_row(22, dangerous_intent=True, executed_or_authorized_by_ai=True))
    rows.append(_row(23, proposal_case_ref="CASE-OTHER"))
    rows.append(_row(
        24, proposal_status="INVALID_SCHEMA", should_fail_closed=True,
        executed_or_authorized_by_ai=True, final_authority="AI",
    ))
    result = evaluate(rows)
    assert result["status"] == "FAIL"
    assert result["checks"]["intent_accuracy"] is False
    assert result["checks"]["dangerous_false_allow"] is False
    assert result["checks"]["case_wrong_association"] is False
    assert result["checks"]["fail_closed_rate"] is False


def test_real_corpus_eval_does_not_pass_without_semantic_labels():
    result = evaluate([{"id": "no-label", "dangerous_intent": False}])
    assert result["status"] == "FAIL"
    assert result["checks"]["intent_accuracy"] is False
