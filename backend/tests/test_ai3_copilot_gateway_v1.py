from __future__ import annotations

from app.copilot import gateway as module
from app.copilot.gateway import CaseCopilotGatewayClient


class _Response:
    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return {
            "model": "copilot-test",
            "proposal": {
                "schema_version": "ai-case-copilot-v1",
                "answer": "当前证据仅支持异常观察，根因尚未确认。",
                "claims": [],
                "cited_evidence_ids": [],
                "uncertainty": ["根因尚未确认"],
                "next_steps": [],
                "root_cause_confirmed_by_ai": False,
                "safety_class": "READ_ONLY_GROUNDED_RESPONSE",
            },
        }


class _Client:
    captured = None

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, url, *, json, headers):
        self.__class__.captured = {"url": url, "json": json, "headers": headers}
        return _Response()


def test_copilot_gateway_redacts_question_and_snapshot_and_keeps_read_only_policy(monkeypatch):
    monkeypatch.setattr(module.httpx, "Client", _Client)
    client = CaseCopilotGatewayClient(
        url="https://gateway.invalid/copilot",
        token="gateway-secret",
        model="copilot-test",
    )
    snapshot = {
        "schema_version": "case-intelligence-snapshot-v1",
        "case": {"case_no": "CASE-1", "summary": "电流音", "status": "ANALYZING"},
        "viewer_role": "ENGINEER",
        "raw_evidence_visible": True,
        "devices": [{
            "id": "dev-1",
            "ip": "192.168.1.253",
            "sn": "SN-SECRET-001",
            "mac": "aa:bb:cc:dd:ee:ff",
            "platform_id": "p1",
            "device_info": {"product": "T18", "password": "device-password"},
        }],
        "evidences": [{"id": "ev-1", "type": "PCAP", "level": "L2", "completeness": "COMPLETE"}],
        "analyzers": {},
        "preliminary_report": None,
        "diagnosis": None,
        "reproductions": [],
        "experiments": [],
        "fix_verifications": [],
        "authority": {"ai_can_confirm_root_cause": False},
        "fingerprint": "f" * 64,
    }
    result = client.answer(
        question="设备 192.168.1.253 / aa:bb:cc:dd:ee:ff 的问题是什么？ password=ask-secret",
        snapshot=snapshot,
    )
    assert result["proposal"]["safety_class"] == "READ_ONLY_GROUNDED_RESPONSE"
    captured = _Client.captured
    rendered = str(captured["json"])
    assert "192.168.1.253" not in rendered
    assert "aa:bb:cc:dd:ee:ff" not in rendered
    assert "SN-SECRET-001" not in rendered
    assert "device-password" not in rendered
    assert "ask-secret" not in rendered
    assert "gateway-secret" not in rendered
    assert captured["headers"]["Authorization"] == "Bearer gateway-secret"
    policy = captured["json"]["policy"]
    assert policy["read_only"] is True
    assert policy["current_case_only"] is True
    assert policy["root_cause_confirmation_forbidden"] is True
    assert policy["evidence_level_promotion_forbidden"] is True
    assert policy["raw_device_commands_forbidden"] is True
    assert policy["control_actions_must_return_control_intent_required"] is True
