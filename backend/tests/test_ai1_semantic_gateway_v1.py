from __future__ import annotations

from app.integrations.feishu import semantic_gateway as module
from app.integrations.feishu.semantic_gateway import SemanticGatewayClient


class _Response:
    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return {
            "model": "semantic-test",
            "proposal": {
                "schema_version": "feishu-semantic-intent-v1",
                "intent": "CASE_FOLLOW_UP",
                "case_operation": "ADD_EVIDENCE",
                "case_ref": None,
                "symptoms": [],
                "device_refs": [],
                "environment_changes": {},
                "temporal_clues": {},
                "attachment_roles": [],
                "comparison_request": {"compare_with_previous_environment": False},
                "requested_operation": "CONTINUE_ANALYSIS",
                "confidence": 0.9,
                "missing_fields": [],
                "safety_class": "NON_EXECUTING_PROPOSAL",
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


def test_semantic_gateway_redacts_sensitive_message_context(monkeypatch):
    monkeypatch.setattr(module.httpx, "Client", _Client)
    client = SemanticGatewayClient(url="https://gateway.invalid/semantic", token="gateway-secret", model="semantic-test")
    text = (
        "设备IP 192.168.1.253\n"
        "MAC aa:bb:cc:dd:ee:ff\n"
        "号码 13800138000\n"
        "password=super-secret\n"
        "又复现了，换回原装就正常"
    )
    result = client.resolve(
        text=text,
        attachments=[{"file_key": "att-1", "filename": "capture.pcap", "message_type": "file"}],
        deterministic={"intent": "CASE_FOLLOW_UP", "confidence": 0.82},
        context={"resolved_case": {"case_id": "case-1", "case_no": "CASE-1"}},
    )
    assert result["proposal"]["safety_class"] == "NON_EXECUTING_PROPOSAL"
    captured = _Client.captured
    rendered = str(captured["json"])
    assert "192.168.1.253" not in rendered
    assert "aa:bb:cc:dd:ee:ff" not in rendered
    assert "13800138000" not in rendered
    assert "super-secret" not in rendered
    assert "gateway-secret" not in rendered
    assert captured["headers"]["Authorization"] == "Bearer gateway-secret"
    assert captured["json"]["policy"]["raw_commands_forbidden"] is True
    assert captured["json"]["policy"]["case_override_forbidden"] is True
    assert captured["json"]["policy"]["rbac_and_policy_recheck_required"] is True
