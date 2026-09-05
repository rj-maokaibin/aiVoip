from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from app.automation.adapters.web_auth.legacy_luci import (
    LegacyLuciAuthProvider,
    PasswordEncoder,
    current_luci_protocol_success,
)


TimestampProvider = Callable[[], str]


@dataclass(frozen=True)
class Apf3260mLuciLoginPayloadBuilder:
    """Source-bound APF3260-M login envelope from the current HAR.

    Password encryption and the concrete timestamp value remain injected runtime
    concerns.  The Automation Core must not guess AES parameters or clock format.
    """

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
    password_encoder: PasswordEncoder,
    timestamp_provider: TimestampProvider,
) -> LegacyLuciAuthProvider:
    """Build the current-product LuCI auth adapter with strict HAR semantics."""

    return LegacyLuciAuthProvider(
        password_encoder=password_encoder,
        login_payload_builder=Apf3260mLuciLoginPayloadBuilder(timestamp_provider),
        protocol_success=current_luci_protocol_success,
    )
