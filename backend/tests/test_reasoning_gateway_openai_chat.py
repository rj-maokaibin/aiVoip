from __future__ import annotations

import httpx

from app.diagnosis.gateway import ReasoningGatewayClient


def test_openai_chat_url_detection_and_effective_endpoint():
    assert ReasoningGatewayClient._is_openai_chat_url("https://uniapi.ruijie.com.cn/v1") is True
    assert ReasoningGatewayClient._is_openai_chat_url("https://uniapi.ruijie.com.cn/v1/chat/completions") is True
    assert ReasoningGatewayClient._is_openai_chat_url("https://gateway.invalid/api/coding/v3") is False
    assert (
        ReasoningGatewayClient._effective_url("https://uniapi.ruijie.com.cn/v1")
        == "https://uniapi.ruijie.com.cn/v1/chat/completions"
    )
    assert (
        ReasoningGatewayClient._effective_url("https://x.io/v1/chat/completions")
        == "https://x.io/v1/chat/completions"
    )


def test_extract_openai_proposal_parses_code_fenced_json():
    data = {
        "choices": [
            {
                "message": {
                    "content": '```json\n{"schema_version": "ai-proposal-v2", "intent": "DIAGNOSIS_ENHANCEMENT"}\n```'
                }
            }
        ]
    }
    proposal = ReasoningGatewayClient._extract_openai_proposal(data)
    assert proposal["schema_version"] == "ai-proposal-v2"
    assert proposal["intent"] == "DIAGNOSIS_ENHANCEMENT"


def test_extract_openai_proposal_accepts_bare_dict_content():
    data = {"choices": [{"message": {"content": {"schema_version": "ai-proposal-v1"}}}]}
    proposal = ReasoningGatewayClient._extract_openai_proposal(data)
    assert proposal["schema_version"] == "ai-proposal-v1"


def test_enhance_sends_openai_chat_format_and_parses_proposal(monkeypatch):
    calls = []

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": '{"schema_version": "ai-proposal-v1", "intent": "DIAGNOSIS_ENHANCEMENT"}'}}]}

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, url, json, headers):
            calls.append((url, json, headers))
            return Response()

    monkeypatch.setattr("app.diagnosis.gateway.httpx.Client", Client)
    monkeypatch.setattr("app.diagnosis.gateway.settings.reasoning_gateway_timeout_seconds", 30)
    client = ReasoningGatewayClient(
        url="https://uniapi.ruijie.com.cn/v1", token="tok", model="deepseek-v4-flash"
    )
    result = client.enhance({"case": {"summary": "noise"}}, {})
    assert len(calls) == 1
    url, payload, headers = calls[0]
    assert url == "https://uniapi.ruijie.com.cn/v1/chat/completions"
    assert payload["model"] == "deepseek-v4-flash"
    assert payload["messages"][0]["role"] == "system"
    assert payload["messages"][1]["role"] == "user"
    assert "context" in payload["messages"][1]["content"]
    assert headers["Authorization"] == "Bearer tok"
    assert result["proposal"]["schema_version"] == "ai-proposal-v1"
    assert result["_routing"]["selected_model"] == "deepseek-v4-flash"


def test_enhance_legacy_custom_url_keeps_custom_payload(monkeypatch):
    calls = []

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"proposal": {"schema_version": "ai-proposal-v1"}}

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, url, json, headers):
            calls.append((url, json))
            return Response()

    monkeypatch.setattr("app.diagnosis.gateway.httpx.Client", Client)
    client = ReasoningGatewayClient(url="https://gateway.invalid/api/coding/v3", model="m")
    result = client.enhance({"case": {"summary": "noise"}}, {})
    assert calls[0][0] == "https://gateway.invalid/api/coding/v3"
    assert calls[0][1]["schema_version"] == "voip-diagnosis-gateway-v2"
    assert result["proposal"]["schema_version"] == "ai-proposal-v1"
