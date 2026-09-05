from __future__ import annotations

from typing import Any, Callable, Mapping

from app.automation.adapters.web_auth.base import WebCredential, WebSession
from app.infrastructure.transport.http import HttpApiTransport, HttpRequest, HttpResponse, HttpRetryPolicy


class LegacyLuciAuthError(RuntimeError):
    pass


PasswordEncoder = Callable[[str], str]
LoginPayloadBuilder = Callable[[str, str], Mapping[str, Any]]
SidExtractor = Callable[[HttpResponse], str | None]


def _default_sid_extractor(response: HttpResponse) -> str | None:
    value = response.json_body
    if not isinstance(value, Mapping):
        return None
    if value.get("sid"):
        return str(value["sid"])
    data = value.get("data")
    if isinstance(data, Mapping) and data.get("sid"):
        return str(data["sid"])
    return None


def _protocol_success(response: HttpResponse) -> bool:
    value = response.json_body
    return bool(
        response.success
        and isinstance(value, Mapping)
        and value.get("code") == 0
        and value.get("error") is None
    )


class LegacyLuciAuthProvider:
    """LuCI login adapter with injected product password encoding.

    The current APF3260-M HAR freezes endpoint, login envelope and sid usage, but
    intentionally does not freeze the password cipher implementation.  The cipher
    and exact timestamp-bearing payload builder therefore remain runtime adapters,
    not Automation Core logic.
    """

    login_endpoint = "/cgi-bin/luci/api/auth"

    def __init__(
        self,
        *,
        password_encoder: PasswordEncoder,
        login_payload_builder: LoginPayloadBuilder,
        sid_extractor: SidExtractor | None = None,
        auth_expired_statuses: tuple[int, ...] = (401, 403),
    ) -> None:
        self.password_encoder = password_encoder
        self.login_payload_builder = login_payload_builder
        self.sid_extractor = sid_extractor or _default_sid_extractor
        self.auth_expired_statuses = auth_expired_statuses

    async def authenticate(
        self,
        transport: HttpApiTransport,
        credential: WebCredential,
    ) -> WebSession:
        encoded_password = self.password_encoder(credential.password)
        payload = dict(self.login_payload_builder(credential.username, encoded_password))
        response = await transport.request(
            HttpRequest(
                method="POST",
                path=self.login_endpoint,
                json_body=payload,
                mutation=False,
                sensitive_values=(credential.password, encoded_password),
            ),
            retry_policy=HttpRetryPolicy(max_attempts=2),
        )
        if not response.success:
            raise LegacyLuciAuthError(f"LEGACY_LUCI_AUTH_HTTP:{response.status_code}")
        if not _protocol_success(response):
            raise LegacyLuciAuthError("LEGACY_LUCI_AUTH_PROTOCOL_REJECTED")
        sid = self.sid_extractor(response)
        if not sid:
            raise LegacyLuciAuthError("LEGACY_LUCI_AUTH_SID_MISSING")
        return WebSession(query={"auth": sid})

    def is_auth_expired(self, response: HttpResponse) -> bool:
        return response.status_code in self.auth_expired_statuses
