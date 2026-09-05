from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx
import pytest

from app.automation.adapters.entries.web import WebEntryAdapter, WebProfileUnboundError
from app.automation.adapters.web_auth.base import SessionManager, WebCredential, WebSession
from app.automation.adapters.web_profiles.schema import TBD_CURRENT_PRODUCT, WebApiProfile, WebApiProfileError
from app.infrastructure.transport.http import (
    HttpApiTransport,
    HttpMutationResultUnknown,
    HttpRequest,
    HttpRetryPolicy,
    mask_http_secrets,
)


class FakeResponse:
    def __init__(self, status_code=200, body=None, text=""):
        self.status_code = status_code
        self._body = body
        self.text = text or ("" if body is None else str(body))
        self.headers = {"content-type": "application/json", "set-cookie": "sid=secret-cookie"}

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def aclose(self):
        pass


def run(coro):
    return asyncio.run(coro)


def test_http_read_retries_but_mutation_timeout_never_blind_retries():
    read_client = FakeClient([httpx.ReadTimeout("x"), FakeResponse(200, {"ok": True})])
    read_transport = HttpApiTransport("http://dut", client=read_client)
    result = run(read_transport.request(HttpRequest(method="GET", path="/api/read"), retry_policy=HttpRetryPolicy(max_attempts=2)))
    assert result.success is True
    assert len(read_client.calls) == 2

    mutation_client = FakeClient([httpx.ReadTimeout("unknown"), FakeResponse(200, {"should": "not happen"})])
    mutation_transport = HttpApiTransport("http://dut", client=mutation_client)
    with pytest.raises(HttpMutationResultUnknown):
        run(mutation_transport.request(HttpRequest(method="POST", path="/api/set", mutation=True)))
    assert len(mutation_client.calls) == 1


def test_http_evidence_masks_headers_cookie_body_and_explicit_sensitive_values():
    client = FakeClient([FakeResponse(200, {"token": "reply-secret", "value": "ok"})])
    transport = HttpApiTransport("http://dut", client=client)
    response = run(transport.request(HttpRequest(
        method="POST",
        path="/login",
        headers={"Authorization": "Bearer top-secret"},
        cookies={"sid": "cookie-secret"},
        json_body={"username": "admin", "password": "pw-secret", "note": "opaque-secret"},
        mutation=True,
        sensitive_values=("opaque-secret",),
    )))
    rendered = str(response.evidence)
    assert "top-secret" not in rendered
    assert "cookie-secret" not in rendered
    assert "pw-secret" not in rendered
    assert "opaque-secret" not in rendered
    assert "reply-secret" not in rendered
    assert mask_http_secrets({"password": "x"}) == {"password": "***"}


class FakeAuth:
    def __init__(self):
        self.auth_calls = 0

    async def authenticate(self, transport, credential):
        self.auth_calls += 1
        assert credential.username == "admin"
        return WebSession(headers={"Authorization": f"Session {self.auth_calls}"})

    def is_auth_expired(self, response):
        return response.status_code in {401, 403}


def test_session_manager_reauths_only_after_explicit_auth_rejection():
    client = FakeClient([FakeResponse(401, {"error": "expired"}), FakeResponse(200, {"ok": True})])
    transport = HttpApiTransport("http://dut", client=client)
    auth = FakeAuth()
    manager = SessionManager(transport, auth, lambda: WebCredential("admin", "pw"))
    response = run(manager.request(HttpRequest(method="GET", path="/status")))
    assert response.status_code == 200
    assert auth.auth_calls == 2
    assert len(client.calls) == 2
    assert client.calls[0][2]["headers"]["Authorization"] == "Session 1"
    assert client.calls[1][2]["headers"]["Authorization"] == "Session 2"


def test_profile_is_strict_and_current_product_voip_binding_remains_tbd():
    profile = WebApiProfile.from_mapping({
        "id": "legacy-luci-v1",
        "auth_provider": "legacy_luci",
        "operations": {
            "voip.account.configure": {
                "endpoint": "/api/rpc",
                "method": "POST",
                "rpc_method": TBD_CURRENT_PRODUCT,
                "mutation": True,
                "readback_operation": "voip.account.read",
            },
            "voip.account.read": {
                "endpoint": "/api/rpc",
                "method": "POST",
                "rpc_method": TBD_CURRENT_PRODUCT,
                "mutation": False,
            },
        },
    })
    assert profile.operation("voip.account.configure").source_bound is False
    with pytest.raises(WebApiProfileError, match="UNKNOWN_FIELDS"):
        WebApiProfile.from_mapping({"id": "x", "auth_provider": "a", "operations": {}, "guess": True})


class StubSessionManager:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def request(self, request, *, retry_policy=None):
        self.calls.append(request)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _bound_profile():
    return WebApiProfile.from_mapping({
        "id": "test",
        "auth_provider": "fake",
        "operations": {
            "voip.account.configure": {
                "endpoint": "/rpc",
                "method": "POST",
                "rpc_method": "voip.set",
                "mutation": True,
                "readback_operation": "voip.account.read",
            },
            "voip.account.read": {
                "endpoint": "/rpc",
                "method": "POST",
                "rpc_method": "voip.get",
                "mutation": False,
            },
        },
    })


def test_web_entry_unknown_mutation_performs_readback_only_and_no_second_mutation():
    evidence = type("E", (), {})()
    unknown = HttpMutationResultUnknown(request_id="r1", evidence=evidence, cause=TimeoutError())
    readback_response = type("R", (), {
        "success": True,
        "status_code": 200,
        "json_body": {"number": "2001"},
        "text": "",
        "evidence": type("E2", (), {})(),
    })()
    session = StubSessionManager([unknown, readback_response])
    adapter = WebEntryAdapter(profile=_bound_profile(), session_manager=session)
    result = run(adapter.execute("voip.account.configure", {"line": 1, "number": "2002"}))
    assert result.unknown_result is True
    assert result.accepted is False
    assert result.readback == {"number": "2001"}
    assert len(session.calls) == 2
    assert session.calls[0].mutation is True
    assert session.calls[1].mutation is False
    assert session.calls[0].path == session.calls[1].path == "/rpc"


def test_unbound_current_product_profile_fails_closed_without_transport_or_ssh_fallback():
    profile = WebApiProfile.from_mapping({
        "id": "unbound",
        "auth_provider": "fake",
        "operations": {
            "voip.account.configure": {
                "endpoint": "/rpc",
                "method": "POST",
                "rpc_method": TBD_CURRENT_PRODUCT,
                "mutation": True,
            }
        },
    })
    session = StubSessionManager([])
    adapter = WebEntryAdapter(profile=profile, session_manager=session)
    with pytest.raises(WebProfileUnboundError, match="NOT_SOURCE_BOUND"):
        run(adapter.execute("voip.account.configure", {"line": 1}))
    assert session.calls == []
