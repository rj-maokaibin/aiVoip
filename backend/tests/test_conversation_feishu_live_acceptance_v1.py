from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "tools/conversation_feishu_live_acceptance.py"


def _load_helper():
    spec = importlib.util.spec_from_file_location("conversation_feishu_live_acceptance_test", HELPER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


live = _load_helper()


def _preflight(tmp_path: Path, **overrides) -> Path:
    payload = {
        "schema_version": 2,
        "contract": "voip-live-acceptance-preflight-v2",
        "status": "PASS",
        "mutation_allowed": True,
        "source_revision": "abc123",
        "runtime_fingerprint": "fingerprint-1",
    }
    payload.update(overrides)
    path = tmp_path / "preflight.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_preflight_requires_v2_pass_mutation_exact_revision_and_runtime_identity(tmp_path: Path):
    path = _preflight(tmp_path)
    payload = live.load_and_validate_preflight(path, expected_revision="abc123")
    assert payload["status"] == "PASS"

    path = _preflight(tmp_path, contract="voip-live-acceptance-preflight-v1")
    with pytest.raises(RuntimeError, match="PREFLIGHT_CONTRACT_MISMATCH"):
        live.load_and_validate_preflight(path, expected_revision="abc123")

    path = _preflight(tmp_path, status="FAIL")
    with pytest.raises(RuntimeError, match="PREFLIGHT_NOT_MUTATION_READY"):
        live.load_and_validate_preflight(path, expected_revision="abc123")

    path = _preflight(tmp_path, mutation_allowed=False)
    with pytest.raises(RuntimeError, match="PREFLIGHT_NOT_MUTATION_READY"):
        live.load_and_validate_preflight(path, expected_revision="abc123")

    path = _preflight(tmp_path, source_revision="different")
    with pytest.raises(RuntimeError, match="PREFLIGHT_REVISION_MISMATCH"):
        live.load_and_validate_preflight(path, expected_revision="abc123")

    path = _preflight(tmp_path, runtime_fingerprint="")
    with pytest.raises(RuntimeError, match="RUNTIME_FINGERPRINT_MISSING"):
        live.load_and_validate_preflight(path, expected_revision="abc123")


def test_live_target_is_dedicated_message_and_requires_explicit_confirmation():
    target = live.validate_live_target(
        message_id="om_1234567890abcdef",
        confirmation=live.CONFIRMATION,
    )
    assert target == "om_1234567890abcdef"

    with pytest.raises(RuntimeError, match="DEDICATED_MESSAGE_ID_REQUIRED"):
        live.validate_live_target(message_id="oc_chat_id", confirmation=live.CONFIRMATION)
    with pytest.raises(RuntimeError, match="EXPLICIT_CONFIRMATION_REQUIRED"):
        live.validate_live_target(message_id="om_1234567890abcdef", confirmation="yes")


def test_live_target_file_must_be_private(tmp_path: Path):
    target = tmp_path / "target"
    target.write_text("om_1234567890abcdef", encoding="utf-8")
    target.chmod(0o600)
    assert live.load_private_target(target, confirmation=live.CONFIRMATION) == "om_1234567890abcdef"

    target.chmod(0o644)
    with pytest.raises(RuntimeError, match="TARGET_FILE_NOT_PRIVATE"):
        live.load_private_target(target, confirmation=live.CONFIRMATION)


def test_live_helper_uses_production_reply_task_and_persisted_sent_trace():
    text = HELPER.read_text(encoding="utf-8")
    assert "reply_feishu_text.apply" in text
    assert "FeishuReplyDeliveryTrace" in text
    assert 'trace.stage != "SENT"' in text
    assert "semantic_reply_key" in text
    assert "CONVERSATION_LIVE_PRODUCTION_ENV_REQUIRED" in text
    assert "CONVERSATION_LIVE_FEISHU_DISABLED" in text
    assert "CONVERSATION_LIVE_CYCLE_DECOUPLING_REQUIRED" in text
    assert "CONVERSATION_LIVE_REPLY_RETRY_REQUIRED" in text


def test_live_result_redacts_real_feishu_message_ids_and_cli_uses_file_target():
    text = HELPER.read_text(encoding="utf-8")
    assert '"source_message_sha256"' in text
    assert '"sent_message_sha256"' in text
    assert 'parser.add_argument("--message-id-file"' in text
    assert 'parser.add_argument("--message-id"' not in text
    assert '"source_message_id":' not in text
    assert '"sent_message_id":' not in text


def test_workflow_is_explicit_only_and_never_mutates_on_pull_request():
    workflow = (ROOT / ".github/workflows/conversation-feishu-live-acceptance.yml").read_text(encoding="utf-8")
    assert "pull_request:" in workflow
    assert "workflow_dispatch:" in workflow
    assert "if: github.event_name == 'workflow_dispatch'" in workflow
    assert "REPLY_TO_DEDICATED_FEISHU_ACCEPTANCE_MESSAGE" in workflow
    assert "expected_head_sha" in workflow
    assert "conversation_feishu_live_acceptance.py" in workflow
    assert "preflight_v2.py" in workflow
    assert '--message-id-file validation/.conversation_live_target' in workflow
    assert '--message-id "$target"' not in workflow


def test_live_acceptance_is_documented_as_separate_from_dut_and_semantic_contract():
    doc = (ROOT / "docs/CONVERSATION_FEISHU_LIVE_ACCEPTANCE_V1.md").read_text(encoding="utf-8")
    assert "Conversation Feishu Live Acceptance V1" in doc
    assert "Conversation Turn" in doc
    assert "Diagnosis Cycle" in doc
    assert "真实 Feishu" in doc
    assert "真实 DUT" in doc
    assert "不等价" in doc
