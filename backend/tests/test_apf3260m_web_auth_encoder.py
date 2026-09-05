from __future__ import annotations

import base64

from app.automation.adapters.web_auth.apf3260m import (
    APF3260M_DEFAULT_AES_PASSPHRASE,
    Apf3260mGibberishAesPasswordEncoder,
    Apf3260mLuciLoginPayloadBuilder,
    build_apf3260m_luci_auth_provider,
)


def test_apf3260m_default_passphrase_matches_source_bound_current_product() -> None:
    assert APF3260M_DEFAULT_AES_PASSPHRASE == "RjYkhwzx$2018!"


def test_apf3260m_gibberish_aes_matches_openssl_compatibility_vector() -> None:
    encoder = Apf3260mGibberishAesPasswordEncoder(
        salt_provider=lambda size: bytes.fromhex("0001020304050607"),
    )

    encoded = encoder("test-password")

    # Generated independently with OpenSSL AES-256-CBC + EVP_BytesToKey(MD5),
    # fixed salt 0001020304050607 and the source-bound APF3260-M passphrase.
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


def test_apf3260m_auth_provider_uses_source_bound_encoder_by_default() -> None:
    provider = build_apf3260m_luci_auth_provider(timestamp_provider=lambda: "1770000000")

    assert isinstance(provider.password_encoder, Apf3260mGibberishAesPasswordEncoder)
