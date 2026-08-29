from app.conversation.response import GroundedConversationResponder


def _snapshot(*, running=False, recommended=None):
    return {
        "case": {
            "case_id": "case-1",
            "case_no": "CASE-CTRL-001",
            "status": "WAITING_USER",
        },
        "runtime": {
            "has_running_work": running,
            "running_job_count": 0,
            "running_analyzer_count": 0,
            "reproduction_state": None,
            "diagnosis_state": "WAITING_USER",
        },
        "diagnosis": {
            "cycle": 3,
            "headline": "候选方向，需补证：pcm_tx 与对应 RTP 媒体路径内容高度一致",
            "blocking_reason": None,
            "manual_action": None,
            "known": [
                "SIP呼叫 2 通，其中已建立/正常结束 1 通",
                "识别到 3 路RTP媒体流",
                "检测到 1 组 PCM↔RTP 高相关媒体映射",
            ],
            "unknown": ["尚不能确认无声的最终根因"],
        },
        "conversation": {
            "active_question": None,
            "recommended_question": recommended,
            "slots": {},
            "unavailable_needs": [],
        },
        "fact_catalog": {},
        "uncertainty_catalog": {},
        "allowed_actions": {
            "FINISH_WITH_PARTIAL_CONCLUSION": "如果暂时无法补充，可按现有证据形成阶段结论。",
            "UPLOAD_RECORDING": "如有现场录音，可上传用于时间对齐和主观现象确认。",
            "UPLOAD_PCAP": "如有新的异常抓包，可继续上传。",
            "REPRODUCE_WHEN_AVAILABLE": "如果现场可复现，可进入受控复现流程。",
        },
        "question_catalog": {},
    }


def test_finish_control_returns_grounded_partial_conclusion_not_ack():
    responder = GroundedConversationResponder()
    text = responder._deterministic_render(
        _snapshot(),
        "CONTROL",
        {
            "intent": "CONTROL",
            "entities": {"control": "FINISH_WITH_PARTIAL_CONCLUSION"},
            "material_diagnostic_context": False,
        },
    )
    assert "当前阶段结论" in text
    assert "已经确认" in text
    assert "仍不能确认" in text
    assert "Root Cause Confirmed" in text
    assert "Resolved" in text
    assert "Fix Verified" in text
    assert "新诊断上下文" not in text


def test_next_action_no_askable_need_answers_what_is_missing():
    responder = GroundedConversationResponder()
    text = responder._deterministic_render(
        _snapshot(
            recommended={
                "id": "partial-conclusion",
                "slot_key": None,
                "text": None,
                "fallback": "系统可以基于现有证据形成阶段结论。",
                "reason": "NO_ASKABLE_NEED",
                "score": 0.0,
            }
        ),
        "CASE_NEXT_ACTION_QUERY",
        {},
    )
    assert "当前没有必须由你补充的信息" in text
    assert "已经确认" in text
    assert "仍未确认" in text
    assert "可选的补充证据" in text
    assert "请按现有证据形成阶段结论" not in text


def test_finish_control_with_running_work_does_not_claim_analysis_finished():
    responder = GroundedConversationResponder()
    text = responder._deterministic_render(
        _snapshot(running=True),
        "CONTROL",
        {
            "intent": "CONTROL",
            "entities": {"control": "FINISH_WITH_PARTIAL_CONCLUSION"},
            "material_diagnostic_context": False,
        },
    )
    assert "当前已启动的分析任务完成后" in text
    assert "不再等待新的外部证据" in text
    assert "Root Cause Confirmed" in text
