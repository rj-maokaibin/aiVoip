from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Protocol

from app.infrastructure.transport.http import HttpApiTransport, HttpRequest, HttpResponse, HttpRetryPolicy


@dataclass(frozen=True)
class WebCredential:
    """Runtime-injected credential. Callers must never persist this object."""
    username: str
    password: str = field(repr=False, compare=False)


@dataclass(frozen=True)
class WebSession:
    headers: Mapping[str, str] = field(default_factory=dict)
    query: Mapping[str, Any] = field(default_factory=dict)
    cookies: Mapping[str, str] = field(default_factory=dict)
    expires_at: datetime | None = None

    def expired(self, now: datetime | None = None) -> bool:
        if self.expires_at is None:
            return False
        return (now or datetime.now(timezone.utc)) >= self.expires_at


class WebAuthProvider(Protocol):
    async def authenticate(self, transport: HttpApiTransport, credential: WebCredential) -> WebSession: ...
    def is_auth_expired(self, response: HttpResponse) -> bool: ...


CredentialProvider = Callable[[], WebCredential]


class SessionManager:
    """Own one WEB session; re-auth only after an explicit auth rejection response."""

    def __init__(self, transport: HttpApiTransport, auth_provider: WebAuthProvider, credential_provider: CredentialProvider) -> None:
        self.transport = transport
        self.auth_provider = auth_provider
        self.credential_provider = credential_provider
        self._session: WebSession | None = None

    def invalidate(self) -> None:
        """Drop only local session state; this performs no network or mutation."""

        self._session = None

    async def ensure_session(self, *, force: bool = False) -> WebSession:
        if force or self._session is None or self._session.expired():
            self._session = await self.auth_provider.authenticate(self.transport, self.credential_provider())
        return self._session

    async def request(self, request: HttpRequest, *, retry_policy: HttpRetryPolicy | None = None) -> HttpResponse:
        session = await self.ensure_session()
        response = await self.transport.request(
            request.with_auth(headers=session.headers, query=session.query, cookies=session.cookies),
            retry_policy=retry_policy,
        )
        if not self.auth_provider.is_auth_expired(response):
            return response
        session = await self.ensure_session(force=True)
        return await self.transport.request(
            request.with_auth(headers=session.headers, query=session.query, cookies=session.cookies),
            retry_policy=retry_policy,
        )
