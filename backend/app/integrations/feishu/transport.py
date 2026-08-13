from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
import time
from typing import Any

import httpx

from app.core.config import settings
from app.integrations.secrets import SecretRef, SecretResolver, SecretResolutionError


class FeishuTransportError(RuntimeError):
    pass


@dataclass(frozen=True)
class FeishuMessageResult:
    message_id: str
    raw: dict[str, Any]


class FeishuCallbackVerifier:
    """Verify Feishu HTTP callback origin without logging callback secrets/body."""

    def _encrypt_key(self) -> str:
        return SecretResolver.resolve(
            SecretRef(value=settings.feishu_encrypt_key, file=settings.feishu_encrypt_key_file, env=settings.feishu_encrypt_key_env),
            name="FEISHU_ENCRYPT_KEY", required=False,
        )

    def _verification_token(self) -> str:
        return SecretResolver.resolve(
            SecretRef(value=settings.feishu_verification_token, file=settings.feishu_verification_token_file, env=settings.feishu_verification_token_env),
            name="FEISHU_VERIFICATION_TOKEN", required=False,
        )

    def verify(self, *, timestamp: str | None, nonce: str | None, signature: str | None, raw_body: bytes, payload: dict[str, Any] | None = None) -> None:
        key = self._encrypt_key()
        token = self._verification_token()
        if key:
            if not timestamp or not nonce or not signature:
                raise FeishuTransportError("FEISHU_CALLBACK_SIGNATURE_REQUIRED")
            digest = hashlib.sha256(str(timestamp).encode() + str(nonce).encode() + key.encode() + raw_body).hexdigest()
            if not hmac.compare_digest(digest, str(signature).lower()):
                raise FeishuTransportError("FEISHU_CALLBACK_SIGNATURE_INVALID")
        elif token:
            candidate = None
            payload = payload or {}
            candidate = payload.get("token")
            if candidate is None and isinstance(payload.get("header"), dict):
                candidate = payload["header"].get("token")
            if not candidate or not hmac.compare_digest(str(candidate), token):
                raise FeishuTransportError("FEISHU_CALLBACK_TOKEN_INVALID")
        else:
            raise FeishuTransportError("FEISHU_CALLBACK_SECURITY_NOT_CONFIGURED")


class FeishuLiveTransport:
    """Minimal official-API transport for the single mutable Case card.

    Credential values are resolved at call time and never persisted in DB/logs.
    The tenant token is cached in-process only and refreshed before expiry.
    """

    _token: str = ""
    _token_expiry: float = 0.0

    def _app_secret(self) -> str:
        try:
            return SecretResolver.resolve(
                SecretRef(value=settings.feishu_app_secret, file=settings.feishu_app_secret_file, env=settings.feishu_app_secret_env),
                name="FEISHU_APP_SECRET", required=True,
            )
        except SecretResolutionError as exc:
            raise FeishuTransportError(str(exc)) from exc

    def configured(self) -> bool:
        try:
            return bool(settings.feishu_app_id and self._app_secret() and settings.feishu_default_receive_id)
        except FeishuTransportError:
            return False

    async def _tenant_token(self) -> str:
        now = time.time()
        if self.__class__._token and now < self.__class__._token_expiry - 60:
            return self.__class__._token
        if not settings.feishu_app_id:
            raise FeishuTransportError("FEISHU_APP_ID_NOT_CONFIGURED")
        url = settings.feishu_base_url.rstrip("/") + "/auth/v3/tenant_access_token/internal"
        async with httpx.AsyncClient(timeout=settings.feishu_timeout_seconds) as client:
            response = await client.post(url, json={"app_id": settings.feishu_app_id, "app_secret": self._app_secret()})
        try:
            data = response.json()
        except Exception as exc:
            raise FeishuTransportError(f"FEISHU_TOKEN_INVALID_RESPONSE:{response.status_code}") from exc
        if response.status_code >= 400 or int(data.get("code", 0)) != 0 or not data.get("tenant_access_token"):
            raise FeishuTransportError(f"FEISHU_TOKEN_FAILED:{data.get('code', response.status_code)}")
        self.__class__._token = str(data["tenant_access_token"])
        self.__class__._token_expiry = now + int(data.get("expire", 7200))
        return self.__class__._token

    async def _request(self, method: str, path: str, *, params: dict[str, str] | None = None, json_body: dict[str, Any] | None = None) -> dict[str, Any]:
        token = await self._tenant_token()
        url = settings.feishu_base_url.rstrip("/") + path
        async with httpx.AsyncClient(timeout=settings.feishu_timeout_seconds) as client:
            response = await client.request(method, url, params=params, json=json_body, headers={"Authorization": f"Bearer {token}"})
        try:
            data = response.json()
        except Exception as exc:
            raise FeishuTransportError(f"FEISHU_API_INVALID_RESPONSE:{response.status_code}") from exc
        if response.status_code >= 400 or int(data.get("code", 0)) != 0:
            # Do not surface request content/tokens in error strings.
            raise FeishuTransportError(f"FEISHU_API_FAILED:{data.get('code', response.status_code)}")
        return data

    async def send_card(self, *, receive_id: str, receive_id_type: str, card: dict[str, Any]) -> FeishuMessageResult:
        data = await self._request(
            "POST", "/im/v1/messages",
            params={"receive_id_type": receive_id_type},
            json_body={"receive_id": receive_id, "msg_type": "interactive", "content": json.dumps(card, ensure_ascii=False)},
        )
        message_id = str(((data.get("data") or {}).get("message_id") or ""))
        if not message_id:
            raise FeishuTransportError("FEISHU_MESSAGE_ID_MISSING")
        return FeishuMessageResult(message_id=message_id, raw=data)

    async def update_card(self, *, message_id: str, card: dict[str, Any]) -> None:
        await self._request(
            "PATCH", f"/im/v1/messages/{message_id}",
            json_body={"content": json.dumps(card, ensure_ascii=False)},
        )
