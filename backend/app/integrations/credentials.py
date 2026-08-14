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

    def __init__(self, secret_file: str | None = None, session_factory=None):
        # Allow the secret file to be injected (e.g. mounted into a container) or fall
        # back to environment variables so the host secret.yaml never has to be copied
        # into the container image. session_factory lets tests inject a DB session;
        # when set, device_credentials (provisioned from Feishu/Poseidon) is preferred.
        self.secret_file = secret_file or os.environ.get("LOCAL_SECRET_FILE", "/home/dev/secret.yaml")
        self._session_factory = session_factory

    def _db_cred(self, *, ip: str, sn: str | None = None) -> dict | None:
        """Look up device_credentials (DB) by ip or sn; return dict or None."""
        if self._session_factory is None:
            return None
        try:
            from app.db.models import DeviceCredential
            from sqlalchemy import select
            db = self._session_factory()
            try:
                row = None
                if sn:
                    row = db.scalar(select(DeviceCredential).where(DeviceCredential.sn == sn))
                if row is None:
                    row = db.scalar(select(DeviceCredential).where(DeviceCredential.ip == ip))
                if row is None:
                    return None
                return {"username": row.username or "root", "password": row.password,
                        "host": row.ip, "sshport": str(row.ssh_port)}
            finally:
                db.close()
        except Exception:
            return None

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
        dev = self._db_cred(ip=ip, sn=sn) if self._session_factory is not None else None
        if dev is None:
            dev = self._find(ip=ip)
        password = str(dev.get("password") or "")
        if not password:
            raise CredentialError("LOCAL_SECRET_DEVICE_MISSING_PASSWORD")
        return password

    def resolve_username(self, *, ip: str, fallback: str | None = None) -> str:
        """Return the device username from the matching DB entry or secret entry.

        The Case device may carry a default username (settings.ssh_username); the
        DB/secret entry is authoritative so real-device auth does not fail on a
        UI default like 'admin'.
        """
        if self._session_factory is not None:
            dev = self._db_cred(ip=ip)
            if dev and dev.get("username"):
                return dev["username"]
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


class PoseidonCredentialProvider(CredentialProvider):
    """Resolve a device SSH password from Poseidon (ops.rj.link) by SN.

    Authenticates through the baichuan SSO center (credentials in ~/secret.yaml
    sso.baichuan) and asks Poseidon devKey for sshpassv1/v2. Used as the DUT
    credential source for the background reproduction platform after a field
    engineer provides SN/IP via Feishu. The password is never returned to the
    engineer and never logged.
    """

    provider_id = "poseidon"
    production_capable = True

    def __init__(self, *, client=None):
        # Optional injected Poseidon client for tests; production builds it per call.
        self._client = client

    async def get_password(self, *, sn: str, ip: str, product: str | None = None) -> str:
        if not sn:
            raise CredentialError("POSEIDON_SN_REQUIRED")
        # Deferred import avoids a cycle (poseidon imports CredentialError from here).
        if self._client is not None:
            client = self._client
        else:
            from app.integrations.poseidon import PoseidonClient
            client = PoseidonClient()
        try:
            v1, v2 = await client.get_ssh_pass(sn=sn, product=product)
        except CredentialError:
            raise
        except Exception as exc:
            raise CredentialError(f"POSEIDON_CREDENTIAL_FAILED:{type(exc).__name__}") from exc
        # Prefer v2; fall back to v1 for older firmware (README_macc_open_ssh).
        password = v2 or v1
        if not password:
            raise CredentialError("POSEIDON_DEVICE_MISSING_PASSWORD")
        return password

    def resolve_username(self, *, ip: str, fallback: str | None = None) -> str:
        # ReyeeOS DUTs are reached as root over SSH (matches the verified APF1250).
        return fallback or "root"


def get_credential_provider() -> CredentialProvider:
    provider = str(settings.credential_provider).lower()
    if provider == "api":
        return ApiCredentialProvider()
    if provider == "local_secret":
        # Prefer device_credentials (DB, provisioned from Feishu/Poseidon) when
        # available; fall back to secret.yaml. session_factory enables DB lookup.
        from app.db.session import SessionLocal
        return LocalSecretCredentialProvider(session_factory=SessionLocal)
    if provider == "poseidon":
        return PoseidonCredentialProvider()
    if provider == "mock":
        return MockCredentialProvider()
    raise CredentialError(f"CREDENTIAL_PROVIDER_UNSUPPORTED:{provider}")
