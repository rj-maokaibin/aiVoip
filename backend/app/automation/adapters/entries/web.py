from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, runtime_checkable

import httpx

from app.automation.adapters.web_auth.base import SessionManager
from app.automation.adapters.web_auth.legacy_luci import LegacyLuciAuthError
from app.automation.adapters.web_profiles.schema import (
    TBD_CURRENT_PRODUCT,
    WebApiProfile,
    WebApiProfileError,
    WebOperationProfile,
)
from app.infrastructure.transport.http import (
    HttpEvidence,
    HttpMutationResultUnknown,
    HttpRequest,
    HttpResponse,
    HttpRetryPolicy,
    mask_http_secrets,
)

_UNKNOWN_OBSERVE_BACKOFF_SECONDS = (0.0, 1.0, 2.0, 4.0)
_UNKNOWN_OBSERVE_ATTEMPT_TIMEOUT_SECONDS = 20.0
_SAFE_OBSERVATION_ERROR_CODE = re.compile(r"^[A-Z0-9_:-]{1,96}$")


def _safe_observation_error_detail(exc: Exception) -> str | None:
    if not isinstance(exc, LegacyLuciAuthError):
        return None
    value = str(exc).strip()
    return value if _SAFE_OBSERVATION_ERROR_CODE.fullmatch(value) else None


_UNKNOWN_OBSERVE_RETRYABLE = (
    LegacyLuciAuthError,
    httpx.TransportError,
    asyncio.TimeoutError,
    TimeoutError,
)


class WebEntryError(RuntimeError):
    pass


class WebProfileUnboundError(WebEntryError):
    pass


@dataclass(frozen=True)
class EntryResult:
    accepted: bool
    status_code: int | None = None
    output: Any | None = None
    evidence: tuple[HttpEvidence, ...] = ()
    unknown_result: bool = False
    readback: Any | None = None
    error: str | None = None
    observation_diagnostics: tuple[Mapping[str, Any], ...] = ()
    # Process-private raw output. Evidence/persistence must use only ``output``.
    # Keep this field last so existing positional construction remains compatible.
    # It exists so reversible WEB mutation can restore exact secret-bearing
    # configuration instead of accidentally writing redaction masks.
    runtime_output: Any | None = field(default=None, repr=False, compare=False)


@runtime_checkable
class EntryAdapter(Protocol):
    async def execute(self, semantic_action: str, args: Mapping[str, Any], ctx: Any) -> EntryResult: ...


@dataclass(frozen=True)
class VoipAccount:
    line: int
    values: Mapping[str, Any] = field(default_factory=dict)


class WebEntryAdapter:
    """WEB entry only. No SSH dependency or fallback path exists in this adapter."""

    def __init__(
        self,
        *,
        profile: WebApiProfile,
        session_manager: SessionManager,
        retry_policy: HttpRetryPolicy | None = None,
    ) -> None:
        self.profile = profile
        self.session_manager = session_manager
        self.retry_policy = retry_policy or HttpRetryPolicy()

    @staticmethod
    def _cmd_array_payload(operation: WebOperationProfile, args: Mapping[str, Any]) -> dict[str, Any]:
        bundle = args.get("bundle", {})
        if operation.mutation and not isinstance(bundle, Mapping):
            raise WebEntryError("WEB_CMD_ARRAY_BUNDLE_MUST_BE_MAPPING")
        items: list[dict[str, Any]] = []
        for item in operation.rpc_items:
            params: dict[str, Any] = {
                "module": item.module,
                "noParse": False,
                "async": None,
                "remoteIp": False,
            }
            if operation.mutation:
                if item.module not in bundle:
                    raise WebEntryError(f"WEB_CMD_ARRAY_BUNDLE_MODULE_MISSING:{item.module}")
                params["data"] = bundle[item.module]
            items.append({"method": item.method, "params": params})
        return {
            "method": "cmdArr",
            "params": {
                "device": str(args.get("device") or "pc"),
                "params": items,
            },
        }

    @staticmethod
    def _payload(operation: WebOperationProfile, args: Mapping[str, Any]) -> Any:
        if not operation.source_bound:
            raise WebProfileUnboundError(
                f"WEB_OPERATION_NOT_SOURCE_BOUND:{operation.semantic_action}"
            )
        if operation.rpc_style == "cmd_array":
            return WebEntryAdapter._cmd_array_payload(operation, args)
        if operation.rpc_method is not None:
            if operation.rpc_method == TBD_CURRENT_PRODUCT:
                raise WebProfileUnboundError(
                    f"WEB_OPERATION_NOT_SOURCE_BOUND:{operation.semantic_action}"
                )
            return {"method": operation.rpc_method, "params": dict(args)}
        return dict(args)

    async def _request_operation(
        self,
        operation: WebOperationProfile,
        args: Mapping[str, Any],
    ) -> HttpResponse:
        return await self.session_manager.request(
            HttpRequest(
                method=operation.method,
                path=operation.endpoint,
                json_body=self._payload(operation, args),
                mutation=operation.mutation,
            ),
            retry_policy=self.retry_policy,
        )

    @staticmethod
    def _cmd_array_result(response: HttpResponse, operation: WebOperationProfile) -> EntryResult:
        body = response.json_body
        protocol_ok = (
            response.success
            and isinstance(body, Mapping)
            and body.get("code") == 0
            and body.get("error") is None
            and isinstance(body.get("data"), list)
            and len(body["data"]) == len(operation.rpc_items)
        )
        raw_data = body.get("data") if isinstance(body, Mapping) else None
        modules: dict[str, Any] = {}
        runtime_modules: dict[str, Any] = {}
        if isinstance(raw_data, list):
            for index, item in enumerate(operation.rpc_items):
                if index >= len(raw_data):
                    break
                value = raw_data[index]
                if isinstance(value, Mapping) and "data" in value:
                    value = value.get("data")
                runtime_modules[item.module] = value
                modules[item.module] = mask_http_secrets(value)

        accepted = protocol_ok
        error: str | None = None
        if not response.success:
            error = f"WEB_HTTP_{response.status_code}"
        elif not protocol_ok:
            error = "WEB_CMD_ARRAY_TOP_LEVEL_REJECTED"
        elif operation.mutation:
            subresults = raw_data if isinstance(raw_data, list) else []
            accepted = all(
                isinstance(item, Mapping)
                and item.get("rcode") == "00000000"
                and item.get("rmsg") == "success"
                for item in subresults
            )
            if not accepted:
                error = "WEB_CMD_ARRAY_SUBREQUEST_REJECTED"

        output = {
            "code": body.get("code") if isinstance(body, Mapping) else None,
            "error": mask_http_secrets(body.get("error")) if isinstance(body, Mapping) else None,
            "modules": modules,
            "request_index_to_module": {
                str(index): item.module for index, item in enumerate(operation.rpc_items)
            },
            "writable_modules": list(operation.writable_modules),
        }
        if operation.mutation and isinstance(raw_data, list):
            output["subresults"] = mask_http_secrets(raw_data)
        return EntryResult(
            accepted=accepted,
            status_code=response.status_code,
            output=output,
            evidence=(response.evidence,),
            error=error,
            runtime_output={"modules": runtime_modules},
        )

    @staticmethod
    def _to_result(response: HttpResponse, operation: WebOperationProfile) -> EntryResult:
        if operation.rpc_style == "cmd_array":
            return WebEntryAdapter._cmd_array_result(response, operation)
        raw_output = response.json_body if response.json_body is not None else response.text
        output = mask_http_secrets(raw_output)
        return EntryResult(
            accepted=response.success,
            status_code=response.status_code,
            output=output,
            evidence=(response.evidence,),
            error=None if response.success else f"WEB_HTTP_{response.status_code}",
        )

    async def _readback_after_unknown(
        self,
        operation: WebOperationProfile,
        args: Mapping[str, Any],
    ) -> tuple[EntryResult | None, tuple[Mapping[str, Any], ...]]:
        diagnostics: list[Mapping[str, Any]] = []
        if not operation.readback_operation:
            return None, tuple(diagnostics)
        readback_op = self.profile.operation(operation.readback_operation)
        if readback_op.mutation:
            raise WebApiProfileError("WEB_READBACK_OPERATION_MUST_BE_READ_ONLY")

        ensure_session = getattr(self.session_manager, "ensure_session", None)
        if not callable(ensure_session):
            # Request-compatible unit-test/future adapter doubles do not own a
            # WEB session. They still get exactly one read-only observation.
            started = time.monotonic()
            try:
                result = self._to_result(
                    await self._request_operation(readback_op, args),
                    readback_op,
                )
            except _UNKNOWN_OBSERVE_RETRYABLE as exc:
                diagnostics.append({
                    "attempt": 1,
                    "phase": "readback",
                    "elapsed_ms": round((time.monotonic() - started) * 1000.0, 3),
                    "status_code": None,
                    "accepted": False,
                    "error": type(exc).__name__,
                    "detail": _safe_observation_error_detail(exc),
                })
                return None, tuple(diagnostics)
            diagnostics.append({
                "attempt": 1,
                "phase": "readback",
                "elapsed_ms": round((time.monotonic() - started) * 1000.0, 3),
                "status_code": result.status_code,
                "accepted": result.accepted,
                "error": result.error,
            })
            return result, tuple(diagnostics)

        invalidate = getattr(self.session_manager, "invalidate", None)
        if callable(invalidate):
            invalidate()

        async def observe_once() -> EntryResult:
            await ensure_session(force=True)
            return self._to_result(
                await self._request_operation(readback_op, args),
                readback_op,
            )

        # A transport-UNKNOWN Save can temporarily invalidate LuCI/auth while
        # the DUT applies configuration. The mutation itself is NEVER retried.
        # Only authentication plus the profile-bound read-only observation may
        # retry. Each observation attempt has an explicit wall-clock budget so
        # nested auth/HTTP retry policies cannot turn a bounded observe window
        # into minutes of runner occupancy. A still-unavailable WEB session
        # degrades to UNKNOWN so mandatory cleanup/reverse verification proceeds.
        last_result: EntryResult | None = None
        for attempt, delay in enumerate(_UNKNOWN_OBSERVE_BACKOFF_SECONDS, start=1):
            if delay:
                await asyncio.sleep(delay)
            started = time.monotonic()
            try:
                last_result = await asyncio.wait_for(
                    observe_once(),
                    timeout=_UNKNOWN_OBSERVE_ATTEMPT_TIMEOUT_SECONDS,
                )
            except _UNKNOWN_OBSERVE_RETRYABLE as exc:
                diagnostics.append({
                    "attempt": attempt,
                    "phase": "reauth_readback",
                    "elapsed_ms": round((time.monotonic() - started) * 1000.0, 3),
                    "status_code": None,
                    "accepted": False,
                    "error": type(exc).__name__,
                    "detail": _safe_observation_error_detail(exc),
                })
                if callable(invalidate):
                    invalidate()
                continue
            diagnostics.append({
                "attempt": attempt,
                "phase": "reauth_readback",
                "elapsed_ms": round((time.monotonic() - started) * 1000.0, 3),
                "status_code": last_result.status_code,
                "accepted": last_result.accepted,
                "error": last_result.error,
            })
            if last_result.accepted:
                return last_result, tuple(diagnostics)
            if callable(invalidate):
                invalidate()
        return last_result, tuple(diagnostics)

    async def execute(
        self,
        semantic_action: str,
        args: Mapping[str, Any],
        ctx: Any = None,
    ) -> EntryResult:
        del ctx
        operation = self.profile.operation(semantic_action)
        try:
            return self._to_result(
                await self._request_operation(operation, args),
                operation,
            )
        except HttpMutationResultUnknown as exc:
            readback, observation_diagnostics = await self._readback_after_unknown(operation, args)
            evidence = [exc.evidence]
            if readback is not None:
                evidence.extend(readback.evidence)
            return EntryResult(
                accepted=False,
                unknown_result=True,
                evidence=tuple(evidence),
                readback=(readback.output if readback is not None else None),
                error=(
                    "HTTP_MUTATION_RESULT_UNKNOWN"
                    if readback is not None and readback.accepted
                    else "HTTP_MUTATION_RESULT_UNKNOWN_OBSERVE_UNAVAILABLE"
                ),
                observation_diagnostics=observation_diagnostics,
            )

    async def configure_voip_account(
        self,
        account: VoipAccount,
        ctx: Any = None,
    ) -> EntryResult:
        return await self.execute(
            "voip.account.configure",
            {"line": account.line, **dict(account.values)},
            ctx,
        )

    async def configure_voip_bundle(
        self,
        bundle: Mapping[str, Any],
        ctx: Any = None,
    ) -> EntryResult:
        return await self.execute("voip.account.configure", {"bundle": dict(bundle)}, ctx)

    async def configure_voip_user_info(
        self,
        value: Any,
        ctx: Any = None,
    ) -> EntryResult:
        return await self.execute(
            "voip.account.configure_user_info",
            {"bundle": {"voipUserInfo": value}},
            ctx,
        )

    async def read_voip_account(self, line: int, ctx: Any = None) -> EntryResult:
        return await self.execute("voip.account.read", {"line": line}, ctx)
