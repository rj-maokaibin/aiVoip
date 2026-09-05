from __future__ import annotations

import base64
import hashlib
import os
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from Crypto.Cipher import AES

from app.automation.adapters.web_auth.legacy_luci import (
    LegacyLuciAuthProvider,
    PasswordEncoder,
    current_luci_protocol_success,
)


TimestampProvider = Callable[[], str]
SaltProvider = Callable[[int], bytes]

APF3260M_DEFAULT_AES_PASSPHRASE = "RjYkhwzx$2018!"
_OPENSSL_SALTED_PREFIX = b"Salted__"
_AES_BLOCK_SIZE = 16


def _evp_bytes_to_key(passphrase: bytes, salt: bytes, output_length: int) -> bytes:
    """OpenSSL/GibberishAES EVP_BytesToKey-compatible MD5 derivation.

    Source binding:
    - current APF3260-M HAR loads GibberishAES and builds the default key as
      ``(window.sctM || "Rj") + GibberishAES.dec(<ciphertext>, "web")``;
    - the HAR ciphertext decrypts to ``Ykhwzx$2018!``;
    - the supplied legacy WEB automation login implementation independently
      uses ``RjYkhwzx$2018!`` with AES-256-CBC, 8-byte salt and OpenSSL
      ``Salted__`` framing.
    """

    if len(salt) != 8:
        raise ValueError("APF3260M_AES_SALT_MUST_BE_8_BYTES")
    derived = b""
    previous = b""
    while len(derived) < output_length:
        previous = hashlib.md5(previous + passphrase + salt).digest()
        derived += previous
    return derived[:output_length]


def _pkcs7_pad(data: bytes) -> bytes:
    pad_length = _AES_BLOCK_SIZE - (len(data) % _AES_BLOCK_SIZE)
    return data + bytes([pad_length]) * pad_length


@dataclass(frozen=True)
class Apf3260mGibberishAesPasswordEncoder:
    """Current APF3260-M WEB password encoder.

    Produces the same OpenSSL-compatible Base64 envelope as GibberishAES:
    ``Base64("Salted__" + salt + AES-256-CBC(PKCS7(password)))``.

    The salt provider is injectable only for deterministic contract tests; live
    runtime uses ``os.urandom`` and therefore never reuses a fixed salt.
    """

    passphrase: str = APF3260M_DEFAULT_AES_PASSPHRASE
    salt_provider: SaltProvider = os.urandom

    def __call__(self, password: str) -> str:
        salt = self.salt_provider(8)
        if not isinstance(salt, (bytes, bytearray)) or len(salt) != 8:
            raise ValueError("APF3260M_AES_SALT_MUST_BE_8_BYTES")
        salt_bytes = bytes(salt)
        key_iv = _evp_bytes_to_key(self.passphrase.encode("utf-8"), salt_bytes, 48)
        key = key_iv[:32]
        iv = key_iv[32:48]
        cipher = AES.new(key, AES.MODE_CBC, iv)
        ciphertext = cipher.encrypt(_pkcs7_pad(password.encode("utf-8")))
        return base64.b64encode(_OPENSSL_SALTED_PREFIX + salt_bytes + ciphertext).decode("ascii")


@dataclass(frozen=True)
class Apf3260mLuciLoginPayloadBuilder:
    """Source-bound APF3260-M login envelope from the current HAR."""

    timestamp_provider: TimestampProvider

    def __call__(self, username: str, encrypted_password: str) -> Mapping[str, Any]:
        timestamp = str(self.timestamp_provider()).strip()
        if not timestamp:
            raise ValueError("APF3260M_LOGIN_TIMESTAMP_REQUIRED")
        if not username:
            raise ValueError("APF3260M_LOGIN_USERNAME_REQUIRED")
        if not encrypted_password:
            raise ValueError("APF3260M_LOGIN_ENCRYPTED_PASSWORD_REQUIRED")
        return {
            "method": "login",
            "params": {
                "username": username,
                "time": timestamp,
                "encry": True,
                "pwd": encrypted_password,
                "isCheckReadAgreement": "true",
            },
        }


def build_apf3260m_luci_auth_provider(
    *,
    timestamp_provider: TimestampProvider,
    password_encoder: PasswordEncoder | None = None,
) -> LegacyLuciAuthProvider:
    """Build the current-product LuCI auth adapter with strict HAR semantics."""

    return LegacyLuciAuthProvider(
        password_encoder=password_encoder or Apf3260mGibberishAesPasswordEncoder(),
        login_payload_builder=Apf3260mLuciLoginPayloadBuilder(timestamp_provider),
        protocol_success=current_luci_protocol_success,
    )
