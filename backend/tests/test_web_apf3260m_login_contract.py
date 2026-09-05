import pytest

from app.automation.adapters.web_auth.apf3260m import (
    Apf3260mLuciLoginPayloadBuilder,
    build_apf3260m_luci_auth_provider,
)
from app.infrastructure.transport.http import HttpEvidence, HttpResponse


def _response(body, status=200):
    evidence = HttpEvidence(
        request_id="login-1",
        attempt=1,
        method="POST",
        path="/cgi-bin/luci/api/auth",
        request={},
        response={},
        elapsed_ms=1.0,
    )
    return HttpResponse(
        status_code=status,
        headers={},
        json_body=body,
        text="",
        request_id="login-1",
        elapsed_ms=1.0,
        evidence=evidence,
    )


def test_apf3260m_login_envelope_matches_current_har_without_hardcoding_cipher():
    builder = Apf3260mLuciLoginPayloadBuilder(timestamp_provider=lambda: "1788577200123")
    payload = builder("admin", "encrypted-runtime-value")

    assert payload == {
        "method": "login",
        "params": {
            "username": "admin",
            "time": "1788577200123",
            "encry": True,
            "pwd": "encrypted-runtime-value",
            "isCheckReadAgreement": "true",
        },
    }


def test_apf3260m_login_envelope_requires_runtime_timestamp_and_encrypted_password():
    with pytest.raises(ValueError, match="APF3260M_LOGIN_TIMESTAMP_REQUIRED"):
        Apf3260mLuciLoginPayloadBuilder(timestamp_provider=lambda: "")(
            "admin", "encrypted-runtime-value"
        )
    with pytest.raises(ValueError, match="APF3260M_LOGIN_ENCRYPTED_PASSWORD_REQUIRED"):
        Apf3260mLuciLoginPayloadBuilder(timestamp_provider=lambda: "1")("admin", "")


def test_apf3260m_auth_factory_enforces_current_application_success_contract():
    provider = build_apf3260m_luci_auth_provider(
        password_encoder=lambda value: f"cipher:{value}",
        timestamp_provider=lambda: "1788577200123",
    )
    assert provider.protocol_success(
        _response({"code": 0, "error": None, "data": {"sid": "S1", "token": "T1"}})
    ) is True
    assert provider.protocol_success(
        _response({"code": 1, "error": "denied", "data": {"sid": "S1"}})
    ) is False
