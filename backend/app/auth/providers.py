from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
import hashlib
import hmac
import time

from app.contracts.enums import UserRole
from app.core.config import settings
from app.core.errors import AppError
from app.integrations.secrets import SecretRef, SecretResolver, SecretResolutionError


@dataclass(frozen=True)
class AuthIdentity:
    actor_id: str
    role: UserRole
    authenticated: bool
    provider: str = "unspecified"


@dataclass(frozen=True)
class AuthRequest:
    actor_id: str | None = None
    actor_role: str | None = None
    timestamp: str | None = None
    signature: str | None = None


class AuthProvider(ABC):
    provider_id: str
    production_capable: bool = False

    @abstractmethod
    def authenticate(self, request: AuthRequest) -> AuthIdentity:
        raise NotImplementedError


class DevelopmentHeaderAuthProvider(AuthProvider):
    provider_id = "dev_headers"
    production_capable = False

    def authenticate(self, request: AuthRequest) -> AuthIdentity:
        if request.actor_id and request.actor_role:
            try:
                role = UserRole(str(request.actor_role).upper())
            except ValueError as exc:
                raise AppError("INVALID_ROLE", details={"role": request.actor_role}) from exc
            return AuthIdentity(request.actor_id, role, True, self.provider_id)
        if settings.auth_allow_anonymous_dev:
            try:
                role = UserRole(settings.auth_default_role.upper())
            except ValueError as exc:
                raise AppError("INVALID_ROLE", details={"role": settings.auth_default_role}) from exc
            return AuthIdentity(settings.auth_default_actor, role, False, self.provider_id)
        raise AppError("AUTH_REQUIRED")


class HmacGatewayAuthProvider(AuthProvider):
    """Production provider for identity asserted by an authenticated gateway.

    The gateway signs actor id, role and unix timestamp with a shared HMAC secret.
    This prevents clients from spoofing the legacy X-Actor-* headers directly.
    """

    provider_id = "gateway_hmac"
    production_capable = True

    def _secret(self) -> str:
        try:
            return SecretResolver.resolve(
                SecretRef(
                    value=settings.auth_gateway_hmac_secret,
                    file=settings.auth_gateway_hmac_secret_file,
                    env=settings.auth_gateway_hmac_secret_env,
                ),
                name="AUTH_GATEWAY_HMAC",
                required=True,
            )
        except SecretResolutionError as exc:
            raise AppError("AUTH_PROVIDER_NOT_CONFIGURED", details={"provider": self.provider_id}) from exc

    @staticmethod
    def _canonical(actor_id: str, role: str, timestamp: str) -> bytes:
        return f"{actor_id}\n{role.upper()}\n{timestamp}".encode("utf-8")

    def authenticate(self, request: AuthRequest) -> AuthIdentity:
        if not all([request.actor_id, request.actor_role, request.timestamp, request.signature]):
            raise AppError("AUTH_REQUIRED")
        try:
            ts = int(str(request.timestamp))
        except ValueError as exc:
            raise AppError("AUTH_SIGNATURE_INVALID") from exc
        skew = abs(int(time.time()) - ts)
        if skew > int(settings.auth_gateway_max_skew_seconds):
            raise AppError("AUTH_SIGNATURE_EXPIRED", details={"max_skew_seconds": settings.auth_gateway_max_skew_seconds})
        try:
            role = UserRole(str(request.actor_role).upper())
        except ValueError as exc:
            raise AppError("INVALID_ROLE", details={"role": request.actor_role}) from exc
        expected = hmac.new(
            self._secret().encode("utf-8"),
            self._canonical(str(request.actor_id), role.value, str(request.timestamp)),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, str(request.signature).lower()):
            raise AppError("AUTH_SIGNATURE_INVALID")
        return AuthIdentity(str(request.actor_id), role, True, self.provider_id)


def get_auth_provider() -> AuthProvider:
    env = settings.app_env.lower()
    configured = str(settings.production_auth_provider or "pending").lower()
    if env in {"development", "test", "e2e"} and configured in {"", "pending", "dev_headers", "trusted_headers_only"}:
        return DevelopmentHeaderAuthProvider()
    if configured == "gateway_hmac":
        return HmacGatewayAuthProvider()
    if configured == "dev_headers" and env != "production":
        return DevelopmentHeaderAuthProvider()
    raise AppError("AUTH_PROVIDER_NOT_CONFIGURED", details={"provider": configured, "app_env": settings.app_env})
