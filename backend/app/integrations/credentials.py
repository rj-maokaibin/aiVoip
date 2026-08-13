from abc import ABC, abstractmethod
import os
from pathlib import Path

import httpx
import yaml

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


class LocalSecretCredentialProvider(CredentialProvider):
    """Resolve a device password from the local /home/dev/secret.yaml file.

    Matches by host:port so a Case whose ip/ssh_port match the secret entry gets the
    real password, without ever storing device passwords in the repo or in application
    logs. This is a local/dev-enablement provider, not the production API provider.
    """

    provider_id = "local_secret"
    production_capable = False

    def __init__(self, secret_file: str | None = None):
        # Allow the secret file to be injected (e.g. mounted into a container) or fall
        # back to environment variables so the host secret.yaml never has to be copied
        # into the container image.
        self.secret_file = secret_file or os.environ.get("LOCAL_SECRET_FILE", "/home/dev/secret.yaml")

    def _env_creds(self) -> dict | None:
        user = os.environ.get("DEV_USER")
        password = os.environ.get("DEV_PASSWORD")
        host = os.environ.get("DEV_HOST")
        if user and password and host:
            return {"username": user, "password": password, "host": host}
        return None

    def _find(self, *, ip: str, port: int | None = None) -> dict:
        env = self._env_creds()
        if env is not None:
            if env["host"] != ip:
                raise CredentialError("LOCAL_SECRET_DEVICE_NOT_FOUND")
            return env
        path = Path(self.secret_file)
        if not path.exists():
            raise CredentialError(f"LOCAL_SECRET_FILE_MISSING:{self.secret_file}")
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            raise CredentialError(f"LOCAL_SECRET_PARSE_FAILED:{type(exc).__name__}") from exc
        devices = data.get("device", data.get("devices", []))
        if isinstance(devices, dict):
            devices = list(devices.values())
        candidates = [d for d in devices if isinstance(d, dict)]
        for dev in candidates:
            host = str(dev.get("host") or "")
            if host != ip:
                continue
            if port is not None:
                try:
                    dev_port = int(dev.get("sshport") or 0)
                except (TypeError, ValueError):
                    dev_port = 0
                if dev_port != port:
                    continue
            return dev
        raise CredentialError("LOCAL_SECRET_DEVICE_NOT_FOUND")

    async def get_password(self, *, sn: str, ip: str, product: str | None = None) -> str:
        dev = self._find(ip=ip)
        password = str(dev.get("password") or "")
        if not password:
            raise CredentialError("LOCAL_SECRET_DEVICE_MISSING_PASSWORD")
        return password

    def resolve_username(self, *, ip: str, fallback: str | None = None) -> str:
        """Return the device username from the matching secret entry.

        The Case device may carry a default username (settings.ssh_username); the
        local secret entry is authoritative so real-device auth does not fail on a
        UI default like 'admin'.
        """
        try:
            dev = self._find(ip=ip)
            user = str(dev.get("username") or "")
            if user:
                return user
        except CredentialError:
            pass
        if fallback:
            return fallback
        raise CredentialError("LOCAL_SECRET_DEVICE_MISSING_USERNAME")


def get_credential_provider() -> CredentialProvider:
    provider = str(settings.credential_provider).lower()
    if provider == "api":
        return ApiCredentialProvider()
    if provider == "local_secret":
        return LocalSecretCredentialProvider()
    if provider == "mock":
        return MockCredentialProvider()
    raise CredentialError(f"CREDENTIAL_PROVIDER_UNSUPPORTED:{provider}")
