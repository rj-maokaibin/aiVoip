from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, runtime_checkable

from app.automation.adapters.web_auth.base import SessionManager
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
    ) -> EntryResult | None:
        if not operation.readback_operation:
            return None
        readback_op = self.profile.operation(operation.readback_operation)
        if readback_op.mutation:
            raise WebApiProfileError("WEB_READBACK_OPERATION_MUST_BE_READ_ONLY")
        # A mutation transport UNKNOWN can leave the LuCI SID stale even when
        # the endpoint still answers HTTP 200 with an application-level auth
        # envelope. SessionManager only auto-refreshes on explicit HTTP 401/403,
        # so production SessionManager instances refresh before the read-only
        # observe step. Lightweight request-compatible stubs/adapters that do not
        # own session state remain supported and simply perform the readback.
        ensure_session = getattr(self.session_manager, "ensure_session", None)
        if callable(ensure_session):
            await ensure_session(force=True)
        return self._to_result(await self._request_operation(readback_op, args), readback_op)

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
            readback = await self._readback_after_unknown(operation, args)
            evidence = [exc.evidence]
            if readback is not None:
                evidence.extend(readback.evidence)
            return EntryResult(
                accepted=False,
                unknown_result=True,
                evidence=tuple(evidence),
                readback=(readback.output if readback is not None else None),
                error="HTTP_MUTATION_RESULT_UNKNOWN",
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

    async def read_voip_account(self, line: int, ctx: Any = None) -> EntryResult:
        return await self.execute("voip.account.read", {"line": line}, ctx)
