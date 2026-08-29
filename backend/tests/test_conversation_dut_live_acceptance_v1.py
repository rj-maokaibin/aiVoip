from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from tools import conversation_dut_live_acceptance as gate


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "conversation-dut-live-acceptance.yml"


def _base_checks(**overrides):
    checks = {
        "feishu_binding": True,
        "dedicated_acceptance_tag": True,
        "feishu_source_message": True,
        "conversation_bound": True,
        "conversation_user_turn": True,
        "conversation_material_context": True,
        "real_dut_session": True,
        "same_case_session": True,
        "fxs_monitor_ready": True,
        "real_call_analyzed": True,
        "capture_analysis_complete": True,
        "cleanup_verified": True,
        "diagnosis_completed": True,
        "completion_feedback_queued": True,
        "post_diagnosis_reply_sent": True,
    }
    checks.update(overrides)
    return checks


class _Target:
    def __init__(self, state="COMPLETED"):
        self.state = state


def test_acceptance_tag_is_explicit_and_bounded():
    assert gate.validate_acceptance_tag("CONV-DUT-E2E-ABCDEF12") == "CONV-DUT-E2E-ABCDEF12"
    for bad in ["", "prod-case", "CONV-DUT-E2E-short", "CONV-DUT-E2E-abc12345", "CONV-DUT-E2E-ABC 12345"]:
        with pytest.raises(RuntimeError, match="TAG_INVALID"):
            gate.validate_acceptance_tag(bad)


def test_gate_waits_for_real_call_instead_of_synthesizing_one():
    checks = _base_checks(real_call_analyzed=False, capture_analysis_complete=False,
                          cleanup_verified=False, diagnosis_completed=False,
                          completion_feedback_queued=False, post_diagnosis_reply_sent=False)
    assert gate._phase(checks=checks, target=_Target("WATCHING"), analyzed_calls=[]) == "WAITING_REAL_CALL"


def test_terminal_session_without_real_analyzed_call_is_blocked():
    checks = _base_checks(real_call_analyzed=False, capture_analysis_complete=False,
                          cleanup_verified=True, diagnosis_completed=False,
                          completion_feedback_queued=False, post_diagnosis_reply_sent=False)
    assert gate._phase(checks=checks, target=_Target("COMPLETED"), analyzed_calls=[]) == "BLOCKED"


def test_gate_requires_cleanup_diagnosis_and_post_diagnosis_reply():
    target = _Target("COMPLETED")
    analyzed = [object()]
    assert gate._phase(
        checks=_base_checks(cleanup_verified=False, diagnosis_completed=False,
                            completion_feedback_queued=False, post_diagnosis_reply_sent=False),
        target=target, analyzed_calls=analyzed,
    ) == "WAITING_CLEANUP"
    assert gate._phase(
        checks=_base_checks(diagnosis_completed=False,
                            completion_feedback_queued=False, post_diagnosis_reply_sent=False),
        target=target, analyzed_calls=analyzed,
    ) == "WAITING_ANALYSIS"
    assert gate._phase(
        checks=_base_checks(completion_feedback_queued=False, post_diagnosis_reply_sent=False),
        target=target, analyzed_calls=analyzed,
    ) == "WAITING_REPLY"
    assert gate._phase(checks=_base_checks(), target=target, analyzed_calls=analyzed) == "PASS"


def test_m7_subset_is_product_flow_not_ai_or_golden_promotion():
    required = set(gate.REQUIRED_M7_KEYS)
    assert {"pcap_present", "pcm_rx_present", "pcm_tx_present", "debug_present"} <= required
    assert {"packet_analyzer_success", "media_analyzer_success", "deterministic_diagnosis_ready"} <= required
    assert {"reproduction_armed", "call_detected", "cleanup_verified", "report_generated"} <= required
    assert "ai_shadow_present" not in required
    assert "ai_grounded" not in required
    assert "golden_materialized" not in required


def test_auditor_source_is_read_only_and_has_no_device_or_synthetic_execution_path():
    source = inspect.getsource(gate)
    forbidden = [
        "db.add(",
        "db.commit(",
        ".apply_async(",
        "execute_shell(",
        "execute_cli(",
        "start_reproduction(",
        "cancel_reproduction(",
        "reply_feishu_text(",
        "ReproductionCall(",
        "ConversationTurn(",
        "Evidence(",
    ]
    for token in forbidden:
        assert token not in source, token
    assert '"synthetic_feishu_turn_created": False' in source
    assert '"synthetic_call_event_created": False' in source
    assert '"dut_action_executed_by_gate": False' in source
    assert '"pbx_action_executed_by_gate": False' in source


def test_live_workflow_is_observer_only_and_exact_master_guarded():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "/run-conversation-dut-e2e " in workflow
    assert "github.event.comment.user.login == github.repository_owner" in workflow
    assert 'test "$(git rev-parse HEAD)" = "$LIVE_EXPECTED_SHA"' in workflow
    assert "CONV-DUT-E2E-[A-Z0-9_-]{8,64}" in workflow
    assert "tools/conversation_dut_live_acceptance.py" in workflow
    for forbidden in [
        "reply_feishu_text",
        "start_reproduction",
        "cancel_reproduction",
        "execute_shell",
        "execute_cli",
        "ssh ",
        "tcpdump -i",
        "voip dsp diag set",
    ]:
        assert forbidden not in workflow, forbidden


def test_source_identifiers_are_sanitized_by_hash():
    assert gate._sha256("om_sensitive")
    assert gate._sha256("om_sensitive") != "om_sensitive"
    assert gate._sha256(None) is None
