from __future__ import annotations

import base64
import hashlib
import os
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from Crypto.Cipher import AES

from app.automation.adapters.web_auth.base import WebCredential, WebSession
from app.automation.adapters.web_auth.legacy_luci import (
    LegacyLuciAuthError,
    LegacyLuciAuthProvider,
    PasswordEncoder,
    current_luci_protocol_success,
)
from app.infrastructure.transport.http import HttpApiTransport, HttpRequest, HttpResponse, HttpRetryPolicy


TimestampProvider = Callable[[], str]
SaltProvider = Callable[[int], bytes]

# Retained only as a legacy/reference compatibility vector. Live APF3260-M auth
# must use the key rendered by the current login page; a read-only DUT probe
# proved that the current rendered key differs from this historical value.
APF3260M_DEFAULT_AES_PASSPHRASE = "RjYkhwzx$2018!"
APF3260M_LOGIN_PAGE = "/cgi-bin/luci/"
_OPENSSL_SALTED_PREFIX = b"Salted__"
_AES_BLOCK_SIZE = 16
_RENDERED_KEY_PATTERNS = (
    re.compile(r"GibberishAES\.enc\s*\(\s*[^,\n]+,\s*([\"'])([^\"']+)\1", re.IGNORECASE),
    re.compile(r"GibberishAES\.enc\s*\(\s*[^,\n]+,\s*`([^`]+)`", re.IGNORECASE),
)


def _evp_bytes_to_key(passphrase: bytes, salt: bytes, output_length: int) -> bytes:
    """OpenSSL/GibberishAES EVP_BytesToKey-compatible MD5 derivation."""

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
    """APF3260-M WEB password encoder.

    Produces the same OpenSSL-compatible Base64 envelope as GibberishAES:
    ``Base64("Salted__" + salt + AES-256-CBC(PKCS7(password)))``.

    Live authentication injects the passphrase rendered by the current login
    page. The historical constant remains available only for deterministic
    compatibility vectors and explicitly injected tests.
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


def extract_apf3260m_rendered_key(page_text: str) -> str | None:
    """Extract the public, page-rendered GibberishAES passphrase.

    The key is intentionally rendered to the browser by the DUT login page and
    is not a credential. We nevertheless keep it process-local and never persist
    or print its value. Current real-DUT evidence proves a 32-character key and
    that using it yields the structured ``sid/sn/token`` login response.
    """

    if not isinstance(page_text, str) or not page_text:
        return None
    for pattern in _RENDERED_KEY_PATTERNS:
        match = pattern.search(page_text)
        if not match:
            continue
        value = match.group(2) if match.lastindex and match.lastindex >= 2 else match.group(1)
        value = str(value).strip()
        if 1 <= len(value) <= 256:
            return value
    return None


def apf3260m_luci_sid_extractor(response: HttpResponse) -> str | None:
    """Extract the current-product LuCI session id from its JSON-RPC envelope."""

    value = response.json_body
    if not isinstance(value, Mapping):
        return None

    direct = value.get("sid")
    if isinstance(direct, str) and direct.strip():
        return direct

    data = value.get("data")
    if isinstance(data, Mapping):
        nested = data.get("sid")
        if isinstance(nested, str) and nested.strip():
            return nested
    elif isinstance(data, str) and data.strip():
        return data

    return None


class Apf3260mLuciAuthProvider(LegacyLuciAuthProvider):
    """APF3260-M auth provider with live page-bound encryption key discovery.

    The current DUT does not use a stable AES passphrase. Before every new WEB
    session this provider performs a non-mutating GET of the login page, extracts
    the browser-visible GibberishAES key, encrypts the runtime password in memory,
    and then executes the normal LuCI login request. Explicitly supplied
    ``password_encoder`` keeps deterministic/reference callers compatible.
    """

    def __init__(
        self,
        *,
        timestamp_provider: TimestampProvider,
        password_encoder: PasswordEncoder | None = None,
    ) -> None:
        self._dynamic_rendered_key = password_encoder is None
        super().__init__(
            password_encoder=password_encoder or Apf3260mGibberishAesPasswordEncoder(),
            login_payload_builder=Apf3260mLuciLoginPayloadBuilder(timestamp_provider),
            sid_extractor=apf3260m_luci_sid_extractor,
            protocol_success=current_luci_protocol_success,
        )

    @property
    def uses_dynamic_rendered_key(self) -> bool:
        return self._dynamic_rendered_key

    async def authenticate(
        self,
        transport: HttpApiTransport,
        credential: WebCredential,
    ) -> WebSession:
        if not self._dynamic_rendered_key:
            return await super().authenticate(transport, credential)

        page_response = await transport.request(
            HttpRequest(
                method="GET",
                path=APF3260M_LOGIN_PAGE,
                mutation=False,
            ),
            retry_policy=HttpRetryPolicy(max_attempts=2),
        )
        if not page_response.success:
            raise LegacyLuciAuthError(
                f"APF3260M_LOGIN_PAGE_HTTP:{page_response.status_code}"
            )

        rendered_key = extract_apf3260m_rendered_key(page_response.text)
        if not rendered_key:
            raise LegacyLuciAuthError("APF3260M_RENDERED_AUTH_KEY_MISSING")

        encoder = Apf3260mGibberishAesPasswordEncoder(passphrase=rendered_key)
        encoded_password = encoder(credential.password)
        payload = dict(self.login_payload_builder(credential.username, encoded_password))
        response = await transport.request(
            HttpRequest(
                method="POST",
                path=self.login_endpoint,
                json_body=payload,
                mutation=False,
                sensitive_values=(credential.password, encoded_password, rendered_key),
            ),
            retry_policy=HttpRetryPolicy(max_attempts=2),
        )

        # Drop local references before evaluating/returning. The raw runtime
        # credential remains owned by the caller's in-memory credential object.
        rendered_key = ""
        encoded_password = ""
        payload = {}

        if not response.success:
            raise LegacyLuciAuthError(f"LEGACY_LUCI_AUTH_HTTP:{response.status_code}")
        if not self.protocol_success(response):
            raise LegacyLuciAuthError("LEGACY_LUCI_AUTH_PROTOCOL_REJECTED")
        sid = self.sid_extractor(response)
        if not sid:
            raise LegacyLuciAuthError("LEGACY_LUCI_AUTH_SID_MISSING")
        return WebSession(query={"auth": sid})


def build_apf3260m_luci_auth_provider(
    *,
    timestamp_provider: TimestampProvider,
    password_encoder: PasswordEncoder | None = None,
) -> LegacyLuciAuthProvider:
    """Build the current-product LuCI auth adapter with fail-closed semantics."""

    return Apf3260mLuciAuthProvider(
        timestamp_provider=timestamp_provider,
        password_encoder=password_encoder,
    )
