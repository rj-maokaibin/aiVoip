from __future__ import annotations

import asyncio
from dataclasses import dataclass
import inspect
import json
from pathlib import Path

import httpx
import pytest

from app.automation.adapters.entries.web import EntryAdapter, WebEntryAdapter, WebProfileUnboundError
from app.automation.adapters.web_auth.base import SessionManager, WebCredential, WebSession
from app.automation.adapters.web_auth.legacy_luci import LegacyLuciAuthProvider
from app.automation.adapters.web_profiles.schema import TBD_CURRENT_PRODUCT, WebApiProfile
from app.infrastructure.transport.http import (
    HttpApiTransport,
    HttpEvidence,
    HttpMutationResultUnknown,
    HttpRequest,
    HttpResponse,
    HttpRetryPolicy,
)


def run(coro):
    return asyncio.run(coro)


class FakeRawResponse:
    def __init__(self, status_code=200, body=None, headers=None, text=None):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}
        self.text = text if text is not None else json.dumps(body if body is not None else {})

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


class FakeHttpClient:
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
        return None


def http_response(status_code, *, body=None, request_id="r1"):
    evidence = HttpEvidence(
        request_id=request_id,
        attempt=1,
        method="POST",
        path="/cmd",
        request={},
        response={"status_code": status_code},
        elapsed_ms=1.0,
    )
    return HttpResponse(
        status_code=status_code,
        headers={},
        json_body=body,
        text=json.dumps(body or {}),
        request_id=request_id,
        elapsed_ms=1.0,
        evidence=evidence,
    )


def test_http_evidence_masks_auth_cookie_password_sid_and_token():
    client = FakeHttpClient([
        FakeRawResponse(
            200,
            {"sid": "server-sid", "nested": {"token": "response-token"}, "ok": True},
            headers={"Set-Cookie": "session=server-secret"},
        )
    ])
    transport = HttpApiTransport("http://dut.local", client=client)
    response = run(transport.request(HttpRequest(
        method="POST",
        path="/api",
        headers={"Authorization": "Bearer abc"},
        query={"auth": "query-sid"},
        cookies={"sid": "cookie-sid"},
        json_body={"user": "2001", "password": "pw", "nested": {"token": "body-token"}},
        mutation=False,
    )))
    dumped = json.dumps({"request": response.evidence.request, "response": response.evidence.response})
    for secret in (
        "Bearer abc",
        "query-sid",
        "cookie-sid",
        "pw",
        "body-token",
        "server-sid",
        "response-token",
        "server-secret",
    ):
        assert secret not in dumped
    assert "***" in dumped


def test_http_read_can_retry_but_mutation_timeout_is_single_attempt_unknown():
    timeout = httpx.ReadTimeout("timeout", request=httpx.Request("POST", "http://dut.local/api"))
    read_client = FakeHttpClient([timeout, FakeRawResponse(200, {"ok": True})])
    read_transport = HttpApiTransport("http://dut.local", client=read_client)
    response = run(read_transport.request(
        HttpRequest(method="POST", path="/api", mutation=False),
        retry_policy=HttpRetryPolicy(max_attempts=2),
    ))
    assert response.success
    assert len(read_client.calls) == 2

    mutation_client = FakeHttpClient([timeout])
    mutation_transport = HttpApiTransport("http://dut.local", client=mutation_client)
    with pytest.raises(HttpMutationResultUnknown):
        run(mutation_transport.request(
            HttpRequest(method="POST", path="/api", mutation=True),
            retry_policy=HttpRetryPolicy(max_attempts=5),
        ))
    assert len(mutation_client.calls) == 1


@dataclass
class FakeAuthProvider:
    auth_calls: int = 0

    async def authenticate(self, transport, credential):
        self.auth_calls += 1
        return WebSession(query={"auth": f"sid-{self.auth_calls}"})

    def is_auth_expired(self, response):
        return response.status_code == 401


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    async def request(self, request, *, retry_policy=None):
        self.requests.append(request)
        return self.responses.pop(0)


def test_session_manager_relogs_once_after_explicit_auth_rejection():
    transport = FakeTransport([http_response(401), http_response(200, body={"ok": True})])
    provider = FakeAuthProvider()
    manager = SessionManager(
        transport,
        provider,
        lambda: WebCredential("admin", "secret"),
    )  # type: ignore[arg-type]
    response = run(manager.request(HttpRequest(method="POST", path="/cmd", mutation=True)))
    assert response.success
    assert provider.auth_calls == 2
    assert [request.query["auth"] for request in transport.requests] == ["sid-1", "sid-2"]


def test_legacy_luci_reference_uses_injected_crypto_and_payload_contract():
    client = FakeHttpClient([FakeRawResponse(200, {"data": {"sid": "S123"}})])
    transport = HttpApiTransport("http://dut.local", client=client)
    encoded_inputs = []

    def encoder(password):
        encoded_inputs.append(password)
        return "AES-CIPHERTEXT"

    provider = LegacyLuciAuthProvider(
        password_encoder=encoder,
        login_payload_builder=lambda username, encrypted: {
            "u": username,
            "encrypted_password": encrypted,
        },
    )
    session = run(provider.authenticate(transport, WebCredential("admin", "plain-secret")))
    assert session.query == {"auth": "S123"}
    assert encoded_inputs == ["plain-secret"]
    assert client.calls[0][1] == "/cgi-bin/luci/api/auth"
    assert client.calls[0][2]["json"] == {
        "u": "admin",
        "encrypted_password": "AES-CIPHERTEXT",
    }


def test_http_explicit_sensitive_values_mask_unknown_product_field_names():
    client = FakeHttpClient([FakeRawResponse(200, {"ok": True})])
    transport = HttpApiTransport("http://dut.local", client=client)
    response = run(transport.request(HttpRequest(
        method="POST",
        path="/auth",
        json_body={"mystery_field": "AES-CIPHERTEXT"},
        sensitive_values=("AES-CIPHERTEXT",),
    )))
    assert "AES-CIPHERTEXT" not in json.dumps(response.evidence.request)


def test_web_profile_keeps_voip_rpc_unbound_until_current_har_or_source():
    profile = WebApiProfile.from_mapping({
        "id": "legacy_luci_v1",
        "auth_provider": "legacy_luci_aes_sid",
        "operations": {
            "voip.account.read": {
                "endpoint": "/cgi-bin/luci/api/cmd",
                "method": "POST",
                "rpc_method": TBD_CURRENT_PRODUCT,
                "mutation": False,
            },
        },
    })
    assert profile.operation("voip.account.read").source_bound is False

    class NeverSession:
        async def request(self, request, *, retry_policy=None):
            raise AssertionError("network must not run for unbound operation")

    adapter = WebEntryAdapter(
        profile=profile,
        session_manager=NeverSession(),
    )  # type: ignore[arg-type]
    with pytest.raises(WebProfileUnboundError, match="NOT_SOURCE_BOUND"):
        run(adapter.execute("voip.account.read", {"line": 1}, None))


def test_web_mutation_unknown_observes_readback_and_never_replays_mutation():
    profile = WebApiProfile.from_mapping({
        "id": "synthetic_source_bound_test",
        "auth_provider": "fake",
        "operations": {
            "voip.account.configure": {
                "endpoint": "/cmd",
                "method": "POST",
                "rpc_method": "source.bound.set",
                "mutation": True,
                "readback_operation": "voip.account.read",
            },
            "voip.account.read": {
                "endpoint": "/cmd",
                "method": "POST",
                "rpc_method": "source.bound.get",
                "mutation": False,
            },
        },
    })

    class UnknownThenReadback:
        def __init__(self):
            self.calls = []

        async def request(self, request, *, retry_policy=None):
            self.calls.append(request)
            if request.mutation:
                evidence = HttpEvidence(
                    request_id="mut-1",
                    attempt=1,
                    method="POST",
                    path="/cmd",
                    request={},
                    response=None,
                    elapsed_ms=1.0,
                    error="ReadTimeout",
                )
                raise HttpMutationResultUnknown(
                    request_id="mut-1",
                    evidence=evidence,
                    cause=TimeoutError("unknown"),
                )
            return http_response(200, body={"number": "2002"}, request_id="read-1")

    session = UnknownThenReadback()
    adapter = WebEntryAdapter(
        profile=profile,
        session_manager=session,
    )  # type: ignore[arg-type]
    result = run(adapter.execute("voip.account.configure", {"number": "2002"}, None))
    assert not result.accepted and result.unknown_result
    assert result.readback == {"number": "2002"}
    assert len(session.calls) == 2
    assert sum(1 for request in session.calls if request.mutation) == 1


def test_web_entry_has_no_ssh_fallback_dependency_and_future_entry_protocol_is_adapter_only():
    source = inspect.getsource(WebEntryAdapter).lower()
    assert "execute_shell" not in source
    assert "sharedssh" not in source

    class FutureMaccAdapter:
        async def execute(self, semantic_action, args, ctx):
            return None

    assert isinstance(FutureMaccAdapter(), EntryAdapter)


def test_legacy_profile_yaml_contains_no_guessed_voip_method():
    profile_path = Path(__file__).resolve().parents[2] / "profiles" / "web_api" / "legacy_luci_v1.yaml"
    raw = profile_path.read_text(encoding="utf-8")
    assert raw.count(TBD_CURRENT_PRODUCT) == 2
    assert "devSta." not in raw
    assert "acConfig." not in raw
