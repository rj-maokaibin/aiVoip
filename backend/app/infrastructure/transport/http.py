from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
import time
from typing import Any, Mapping, Protocol
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

_SECRET_KEYS = {"auth", "authorization", "cookie", "csrf", "passwd", "password", "password_ref", "encrypted_password", "secret", "set-cookie", "sid", "token"}
_MASK = "***"


def mask_http_secrets(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): (_MASK if str(k).lower() in _SECRET_KEYS else mask_http_secrets(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [mask_http_secrets(v) for v in value]
    if isinstance(value, tuple):
        return tuple(mask_http_secrets(v) for v in value)
    return value


def _secret_values(value: Any, *, key: str | None = None) -> set[str]:
    found: set[str] = set()
    if key and key.lower() in _SECRET_KEYS and value is not None:
        found.add(str(value))
        return found
    if isinstance(value, Mapping):
        for child_key, child in value.items():
            found.update(_secret_values(child, key=str(child_key)))
    elif isinstance(value, (list, tuple)):
        for child in value:
            found.update(_secret_values(child))
    return {item for item in found if item and item != _MASK}


def _redact_text(text: str, secrets: set[str]) -> str:
    for secret in sorted(secrets, key=len, reverse=True):
        text = text.replace(secret, _MASK)
    return text


@dataclass(frozen=True)
class HttpRetryPolicy:
    max_attempts: int = 3
    backoff_seconds: float = 0.0
    retry_statuses: tuple[int, ...] = (502, 503, 504)

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("HTTP_MAX_ATTEMPTS_INVALID")
        if self.backoff_seconds < 0:
            raise ValueError("HTTP_BACKOFF_INVALID")


@dataclass(frozen=True)
class HttpRequest:
    method: str
    path: str
    headers: Mapping[str, str] = field(default_factory=dict)
    query: Mapping[str, Any] = field(default_factory=dict)
    cookies: Mapping[str, str] = field(default_factory=dict)
    json_body: Any | None = None
    data: Any | None = None
    mutation: bool = False
    connect_timeout: float = 5.0
    read_timeout: float = 15.0
    request_id: str | None = None
    sensitive_values: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.scheme or parsed.netloc or not self.path.startswith("/"):
            raise ValueError("HTTP_PATH_MUST_BE_RELATIVE_ABSOLUTE_PATH")
        if self.connect_timeout <= 0 or self.read_timeout <= 0:
            raise ValueError("HTTP_TIMEOUT_INVALID")

    def with_auth(self, *, headers=None, query=None, cookies=None) -> "HttpRequest":
        return HttpRequest(
            method=self.method,
            path=self.path,
            headers={**dict(self.headers), **dict(headers or {})},
            query={**dict(self.query), **dict(query or {})},
            cookies={**dict(self.cookies), **dict(cookies or {})},
            json_body=self.json_body,
            data=self.data,
            mutation=self.mutation,
            connect_timeout=self.connect_timeout,
            read_timeout=self.read_timeout,
            request_id=self.request_id,
            sensitive_values=self.sensitive_values,
        )


@dataclass(frozen=True)
class HttpEvidence:
    request_id: str
    attempt: int
    method: str
    path: str
    request: Mapping[str, Any]
    response: Mapping[str, Any] | None
    elapsed_ms: float
    error: str | None = None


@dataclass(frozen=True)
class HttpResponse:
    status_code: int
    headers: Mapping[str, str]
    json_body: Any | None
    text: str
    request_id: str
    elapsed_ms: float
    evidence: HttpEvidence

    @property
    def success(self) -> bool:
        return 200 <= self.status_code < 300


class HttpTransportError(RuntimeError):
    pass


class HttpMutationResultUnknown(HttpTransportError):
    def __init__(self, *, request_id: str, evidence: HttpEvidence, cause: Exception):
        super().__init__(f"HTTP_MUTATION_RESULT_UNKNOWN:{type(cause).__name__}")
        self.request_id = request_id
        self.evidence = evidence
        self.cause = cause


class HttpClientProtocol(Protocol):
    async def request(self, method: str, url: str, **kwargs: Any) -> Any: ...
    async def aclose(self) -> None: ...


class HttpApiTransport:
    """Business-agnostic HTTP transport with masked evidence and no blind mutation retry."""

    def __init__(self, base_url: str, *, client: HttpClientProtocol | None = None, default_retry_policy: HttpRetryPolicy | None = None) -> None:
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("HTTP_BASE_URL_INVALID")
        self.base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self._client: HttpClientProtocol = client or httpx.AsyncClient(base_url=self.base_url)
        self.default_retry_policy = default_retry_policy or HttpRetryPolicy()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @staticmethod
    def _transient(exc: Exception) -> bool:
        return isinstance(exc, (TimeoutError, asyncio.TimeoutError, httpx.TimeoutException, httpx.TransportError))

    @staticmethod
    def _response_json(response: Any) -> Any | None:
        try:
            return response.json()
        except (TypeError, ValueError, json.JSONDecodeError):
            return None

    def _request_evidence(self, req: HttpRequest) -> tuple[dict[str, Any], set[str]]:
        raw = {"headers": dict(req.headers), "query": dict(req.query), "cookies": dict(req.cookies), "json": req.json_body, "data": req.data}
        secrets = _secret_values(raw) | {str(v) for v in req.sensitive_values if v}
        masked = mask_http_secrets(raw)

        def scrub(value: Any) -> Any:
            if isinstance(value, Mapping):
                return {str(k): scrub(v) for k, v in value.items()}
            if isinstance(value, list):
                return [scrub(v) for v in value]
            if isinstance(value, tuple):
                return tuple(scrub(v) for v in value)
            return _MASK if str(value) in secrets else value

        return scrub(masked), secrets

    @staticmethod
    def _response_evidence(response: Any, json_body: Any | None, secrets: set[str]) -> dict[str, Any]:
        headers = mask_http_secrets(dict(response.headers))
        body = mask_http_secrets(json_body) if json_body is not None else _redact_text(str(response.text), secrets)
        return {"status_code": int(response.status_code), "headers": headers, "body": body}

    async def request(self, req: HttpRequest, *, retry_policy: HttpRetryPolicy | None = None) -> HttpResponse:
        policy = retry_policy or self.default_retry_policy
        attempts = 1 if req.mutation else policy.max_attempts
        request_id = req.request_id or str(uuid4())
        request_evidence, secret_values = self._request_evidence(req)
        last_exc: Exception | None = None

        for attempt in range(1, attempts + 1):
            started = time.monotonic()
            try:
                headers = dict(req.headers)
                headers.setdefault("X-Request-ID", request_id)
                timeout = httpx.Timeout(connect=req.connect_timeout, read=req.read_timeout, write=req.read_timeout, pool=req.connect_timeout)
                response = await self._client.request(
                    req.method.upper(), req.path, headers=headers, params=dict(req.query), cookies=dict(req.cookies),
                    json=req.json_body, content=req.data, timeout=timeout,
                )
                elapsed_ms = (time.monotonic() - started) * 1000.0
                json_body = self._response_json(response)
                evidence = HttpEvidence(
                    request_id=request_id, attempt=attempt, method=req.method.upper(), path=req.path,
                    request=request_evidence, response=self._response_evidence(response, json_body, secret_values), elapsed_ms=elapsed_ms,
                )
                if not req.mutation and int(response.status_code) in policy.retry_statuses and attempt < attempts:
                    if policy.backoff_seconds:
                        await asyncio.sleep(policy.backoff_seconds * attempt)
                    continue
                return HttpResponse(
                    status_code=int(response.status_code), headers=dict(response.headers), json_body=json_body,
                    text=str(response.text), request_id=request_id, elapsed_ms=elapsed_ms, evidence=evidence,
                )
            except Exception as exc:
                elapsed_ms = (time.monotonic() - started) * 1000.0
                evidence = HttpEvidence(
                    request_id=request_id, attempt=attempt, method=req.method.upper(), path=req.path,
                    request=request_evidence, response=None, elapsed_ms=elapsed_ms, error=type(exc).__name__,
                )
                if req.mutation and self._transient(exc):
                    raise HttpMutationResultUnknown(request_id=request_id, evidence=evidence, cause=exc) from exc
                if not self._transient(exc) or attempt >= attempts:
                    raise
                last_exc = exc
                if policy.backoff_seconds:
                    await asyncio.sleep(policy.backoff_seconds * attempt)

        assert last_exc is not None
        raise last_exc
