import pytest

from app.automation.adapters.web_auth.apf3260m import Apf3260mLuciLoginPayloadBuilder


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
