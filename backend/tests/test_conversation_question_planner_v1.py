from app.conversation.planner import select_user_question


def _decision(*needs):
    return {
        "plan": [
            {
                "action_type": "REQUEST_USER_EVIDENCE",
                "params": {"need": list(needs)},
            }
        ]
    }


def test_timestamp_wins_when_all_needs_available():
    plan = select_user_question(decision=_decision("pcap", "anomaly_timestamp", "recording"))
    assert plan.kind == "QUESTION"
    assert plan.need == "anomaly_timestamp"
    assert "异常发生的大致时间" in plan.question


def test_unknown_timestamp_is_suppressed_and_switches_to_pcap():
    plan = select_user_question(
        decision=_decision("anomaly_timestamp", "pcap", "recording"),
        slots={"anomaly_timestamp": {"state": "UNKNOWN_BY_USER", "asked_count": 1}},
        unavailable_needs=["anomaly_timestamp"],
    )
    assert plan.kind == "QUESTION"
    assert plan.need == "pcap"


def test_unavailable_pcap_and_timestamp_switch_to_recording():
    plan = select_user_question(
        decision=_decision("anomaly_timestamp", "pcap", "recording"),
        slots={
            "anomaly_timestamp": {"state": "UNKNOWN_BY_USER"},
            "pcap": {"state": "UNAVAILABLE"},
        },
        unavailable_needs=["anomaly_timestamp", "pcap"],
    )
    assert plan.kind == "QUESTION"
    assert plan.need == "recording"


def test_all_needs_unavailable_returns_partial_conclusion():
    plan = select_user_question(
        decision=_decision("anomaly_timestamp", "pcap"),
        slots={
            "anomaly_timestamp": {"state": "UNKNOWN_BY_USER"},
            "pcap": {"state": "UNAVAILABLE"},
        },
        unavailable_needs=["anomaly_timestamp", "pcap"],
    )
    assert plan.kind == "PARTIAL_CONCLUSION"
    assert plan.question is None
    assert "系统可以直接基于现有证据形成阶段结论" in plan.fallback
    assert "请按现有证据形成阶段结论" not in plan.fallback


def test_terminal_blocker_never_asks_another_question():
    plan = select_user_question(
        decision=_decision("anomaly_timestamp"),
        summary={"blocking_reason": "MAX_CYCLES"},
    )
    assert plan.kind == "PARTIAL_CONCLUSION"
    assert plan.reason == "MAX_CYCLES"
    assert "系统将基于现有证据形成阶段结论" in plan.fallback


def test_user_finish_control_suppresses_all_future_questions():
    plan = select_user_question(
        decision=_decision("anomaly_timestamp", "pcap", "recording"),
        slots={
            "__conversation_control__": {
                "state": "FINISH_WITH_PARTIAL_CONCLUSION",
                "source": "USER_CONTROL",
            }
        },
    )
    assert plan.kind == "PARTIAL_CONCLUSION"
    assert plan.reason == "USER_REQUESTED_PARTIAL_CONCLUSION"
    assert plan.question is None
    assert "不再等待新的用户证据" in plan.fallback
