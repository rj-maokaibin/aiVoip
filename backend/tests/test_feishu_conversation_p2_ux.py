from __future__ import annotations

from app.conversation.response import GroundedConversationResponder
from app.integrations.feishu.case_boundary import _new_case_summary


def _snapshot(*, running: bool = False) -> dict:
    return {
        "case": {
            "case_no": "VOIP-20260830-P2UX01",
            "status": "WAITING_USER",
        },
        "runtime": {
            "has_running_work": running,
        },
        "diagnosis": {
            "headline": "候选方向，需补证：pcm_tx 与对应RTP媒体路径内容高度一致",
            "known": [
                "SIP呼叫 2 通，其中已建立/正常结束 1 通。",
                "识别到 3 路RTP媒体流。；",
                "检测到 1 组 PCM↔RTP 高相关媒体映射。",
            ],
            "unknown": [
                "当前证据不足以确认单通无声的根因层级。",
            ],
            "blocking_reason": None,
        },
        "conversation": {
            "active_question": None,
            "recommended_question": {
                "id": "partial-conclusion",
                "text": None,
            },
        },
        "allowed_actions": {
            "FINISH_WITH_PARTIAL_CONCLUSION": "可以按现有证据形成阶段结论。",
            "UPLOAD_RECORDING": "如有现场录音，可上传用于时间对齐和主观现象确认。",
            "UPLOAD_PCAP": "如有新的异常抓包，可继续上传。",
            "REPRODUCE_WHEN_AVAILABLE": "如果现场可复现，可进入受控复现流程。",
        },
    }


def test_new_case_summary_strips_boundary_control_but_keeps_symptom():
    text = "这是新的故障，不要关联上一个 Case。另一台设备出现单通无声，请新建一个 Case 开始分析。"
    assert _new_case_summary(text, []) == "另一台设备出现单通无声"


def test_new_case_summary_does_not_rewrite_normal_incident():
    text = "客户现场偶发单通无声，重拨后恢复"
    assert _new_case_summary(text, []) == text


def test_pure_new_case_command_still_uses_safe_fallback():
    assert _new_case_summary("新建 Case", []) == "飞书新故障（待补充现象）"


def test_partial_conclusion_is_structured_and_hides_internal_policy_wording():
    text = GroundedConversationResponder._partial_conclusion(_snapshot())
    assert "当前阶段结论：" in text
    assert "\n\n已确认\n• SIP呼叫 2 通" in text
    assert "\n\n尚未确认\n• 当前证据不足以确认单通无声的根因层级" in text
    assert "\n\n后续可选\n• 如有现场录音" in text
    assert "边界说明：" in text
    assert "当前允许集合" not in text
    assert "。；" not in text


def test_finish_control_ack_explains_case_remains_open():
    responder = object.__new__(GroundedConversationResponder)
    text = responder._deterministic_render(
        _snapshot(),
        "CONTROL",
        {"entities": {"control": "FINISH_WITH_PARTIAL_CONCLUSION"}},
    )
    assert "已结束本轮主动分析" in text
    assert "Case VOIP-20260830-P2UX01 仍保持打开" in text
    assert "已确认" in text
    assert "尚未确认" in text
    assert "后续可选" in text


def test_continue_control_ack_explicitly_confirms_resumed_state():
    responder = object.__new__(GroundedConversationResponder)
    snapshot = _snapshot()
    snapshot["conversation"]["recommended_question"] = {}
    text = responder._deterministic_render(
        snapshot,
        "CONTROL",
        {"entities": {"control": "CONTINUE_ANALYSIS"}},
    )
    assert text.startswith("已恢复 Case VOIP-20260830-P2UX01 的分析状态。")
    assert "当前没有必须由你补充的信息" in text
    assert "新的后台任务需要重复启动" in text


def test_progress_query_uses_user_facing_state_instead_of_raw_enum():
    responder = object.__new__(GroundedConversationResponder)
    text = responder._deterministic_render(_snapshot(), "CASE_PROGRESS_QUERY", {})
    assert "当前状态：本轮分析已完成，可形成阶段结论" in text
    assert "后台任务：无" in text
    assert "WAITING_USER" not in text
    assert "已确认" in text
    assert "尚未确认" in text
