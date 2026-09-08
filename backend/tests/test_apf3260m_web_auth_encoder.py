from __future__ import annotations

import asyncio
import base64
from types import SimpleNamespace

from app.automation.adapters.web_auth.apf3260m import (
    APF3260M_DEFAULT_AES_PASSPHRASE,
    APF3260M_LOGIN_PAGE,
    Apf3260mGibberishAesPasswordEncoder,
    Apf3260mLuciAuthProvider,
    Apf3260mLuciLoginPayloadBuilder,
    apf3260m_luci_sid_extractor,
    build_apf3260m_luci_auth_provider,
    extract_apf3260m_rendered_key,
)
from app.automation.adapters.web_auth.base import WebCredential


def test_apf3260m_legacy_passphrase_matches_reference_vector() -> None:
    assert APF3260M_DEFAULT_AES_PASSPHRASE == "RjYkhwzx$2018!"


def test_apf3260m_gibberish_aes_matches_openssl_compatibility_vector() -> None:
    encoder = Apf3260mGibberishAesPasswordEncoder(
        salt_provider=lambda size: bytes.fromhex("0001020304050607"),
    )

    encoded = encoder("test-password")

    assert encoded == "U2FsdGVkX18AAQIDBAUGBy7VOyPPxRfL/q3swjqVFXs="
    decoded = base64.b64decode(encoded)
    assert decoded[:8] == b"Salted__"
    assert decoded[8:16] == bytes.fromhex("0001020304050607")


def test_apf3260m_current_login_payload_uses_har_pwd_field_not_legacy_password() -> None:
    payload = Apf3260mLuciLoginPayloadBuilder(lambda: "1770000000")(
        "admin",
        "ciphertext",
    )

    assert payload == {
        "method": "login",
        "params": {
            "username": "admin",
            "time": "1770000000",
            "encry": True,
            "pwd": "ciphertext",
            "isCheckReadAgreement": "true",
        },
    }
    assert "password" not in payload["params"]


def test_apf3260m_rendered_key_extractor_accepts_current_browser_call_shape() -> None:
    rendered = "0123456789abcdef0123456789abcdef"
    page = f'<script>var payload = GibberishAES.enc(password, "{rendered}");</script>'

    assert extract_apf3260m_rendered_key(page) == rendered
    assert rendered != APF3260M_DEFAULT_AES_PASSPHRASE


def test_apf3260m_rendered_key_extractor_fails_closed_when_missing() -> None:
    assert extract_apf3260m_rendered_key("<html>no key here</html>") is None
    assert extract_apf3260m_rendered_key("") is None


def test_apf3260m_sid_extractor_accepts_source_bound_scalar_data_session() -> None:
    response = SimpleNamespace(json_body={"code": 0, "error": None, "data": "session-value"})

    assert apf3260m_luci_sid_extractor(response) == "session-value"


def test_apf3260m_sid_extractor_keeps_structured_sid_compatibility() -> None:
    top_level = SimpleNamespace(json_body={"sid": "top-level"})
    nested = SimpleNamespace(json_body={"data": {"sid": "nested"}})

    assert apf3260m_luci_sid_extractor(top_level) == "top-level"
    assert apf3260m_luci_sid_extractor(nested) == "nested"


def test_apf3260m_sid_extractor_does_not_treat_non_string_data_as_session() -> None:
    assert apf3260m_luci_sid_extractor(SimpleNamespace(json_body={"data": True})) is None
    assert apf3260m_luci_sid_extractor(SimpleNamespace(json_body={"data": ""})) is None


class _FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    async def request(self, request, *, retry_policy=None):
        self.requests.append((request, retry_policy))
        return self.responses.pop(0)


def test_apf3260m_auth_provider_fetches_rendered_key_before_login() -> None:
    rendered = "0123456789abcdef0123456789abcdef"
    page_response = SimpleNamespace(
        success=True,
        status_code=200,
        text=f'GibberishAES.enc(password, "{rendered}")',
        json_body=None,
    )
    login_response = SimpleNamespace(
        success=True,
        status_code=200,
        text="",
        json_body={
            "code": 0,
            "error": None,
            "data": {"sid": "session-1", "sn": "device-sn", "token": "token-1"},
        },
    )
    transport = _FakeTransport([page_response, login_response])
    provider = build_apf3260m_luci_auth_provider(timestamp_provider=lambda: "1770000000")

    session = asyncio.run(
        provider.authenticate(transport, WebCredential(username="admin", password="runtime-password"))
    )

    assert isinstance(provider, Apf3260mLuciAuthProvider)
    assert provider.uses_dynamic_rendered_key is True
    assert len(transport.requests) == 2
    page_request = transport.requests[0][0]
    login_request = transport.requests[1][0]
    assert page_request.method == "GET"
    assert page_request.path == APF3260M_LOGIN_PAGE
    assert page_request.mutation is False
    assert login_request.method == "POST"
    assert login_request.path == provider.login_endpoint
    assert login_request.mutation is False
    assert login_request.json_body["params"]["username"] == "admin"
    assert login_request.json_body["params"]["pwd"] != "runtime-password"
    assert session.query == {"auth": "session-1"}


def test_apf3260m_auth_provider_keeps_explicit_encoder_compatibility() -> None:
    encoder = Apf3260mGibberishAesPasswordEncoder(
        passphrase="0123456789abcdef0123456789abcdef",
        salt_provider=lambda size: bytes.fromhex("0001020304050607"),
    )
    provider = build_apf3260m_luci_auth_provider(
        timestamp_provider=lambda: "1770000000",
        password_encoder=encoder,
    )

    assert isinstance(provider, Apf3260mLuciAuthProvider)
    assert provider.uses_dynamic_rendered_key is False
    assert provider.password_encoder is encoder
    assert provider.sid_extractor is apf3260m_luci_sid_extractor
