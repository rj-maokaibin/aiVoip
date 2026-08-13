from abc import ABC, abstractmethod
import httpx

from app.core.config import settings
from app.integrations.secrets import SecretRef, SecretResolver, SecretResolutionError


class CredentialError(RuntimeError):
    pass


class CredentialProvider(ABC):
    provider_id: str
    production_capable: bool = False

    @abstractmethod
    async def get_password(self, *, sn: str, ip: str, product: str | None = None) -> str:
        raise NotImplementedError


class MockCredentialProvider(CredentialProvider):
    provider_id = "mock"
    production_capable = False

    async def get_password(self, *, sn: str, ip: str, product: str | None = None) -> str:
        if not settings.mock_device_password or settings.mock_device_password == "change-me":
            raise CredentialError("MOCK_DEVICE_PASSWORD_NOT_CONFIGURED")
        return settings.mock_device_password


class ApiCredentialProvider(CredentialProvider):
    provider_id = "api"
    production_capable = True

    def _token(self) -> str:
        try:
            return SecretResolver.resolve(
                SecretRef(
                    value=settings.credential_api_token,
                    file=settings.credential_api_token_file,
                    env=settings.credential_api_token_env,
                ),
                name="CREDENTIAL_API_TOKEN",
                required=False,
            )
        except SecretResolutionError as exc:
            raise CredentialError(str(exc)) from exc

    async def get_password(self, *, sn: str, ip: str, product: str | None = None) -> str:
        if not settings.credential_api_url:
            raise CredentialError("CREDENTIAL_API_URL_NOT_CONFIGURED")
        headers = {}
        token = self._token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        payload = {"sn": sn, "ip": ip}
        if product:
            payload["product"] = product
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.post(settings.credential_api_url, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            # Never include response body/token/password in the raised error.
            raise CredentialError(f"CREDENTIAL_API_FAILED:{type(exc).__name__}") from exc
        password = data.get("password")
        if not password:
            raise CredentialError("CREDENTIAL_API_MISSING_PASSWORD")
        return str(password)


def get_credential_provider() -> CredentialProvider:
    provider = str(settings.credential_provider).lower()
    if provider == "api":
        return ApiCredentialProvider()
    if provider == "mock":
        return MockCredentialProvider()
    raise CredentialError(f"CREDENTIAL_PROVIDER_UNSUPPORTED:{provider}")
