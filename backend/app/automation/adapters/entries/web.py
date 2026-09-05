from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, runtime_checkable

from app.automation.adapters.web_auth.base import SessionManager
from app.automation.adapters.web_profiles.schema import TBD_CURRENT_PRODUCT, WebApiProfile, WebApiProfileError, WebOperationProfile
from app.infrastructure.transport.http import HttpEvidence, HttpMutationResultUnknown, HttpRequest, HttpResponse, HttpRetryPolicy


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


@runtime_checkable
class EntryAdapter(Protocol):
    async def execute(self, semantic_action: str, args: Mapping[str, Any], ctx: Any) -> EntryResult: ...


@dataclass(frozen=True)
class VoipAccount:
    line: int
    values: Mapping[str, Any] = field(default_factory=dict)


class WebEntryAdapter:
    """WEB entry only. No SSH dependency or fallback path exists in this adapter."""

    def __init__(self, *, profile: WebApiProfile, session_manager: SessionManager,
                 retry_policy: HttpRetryPolicy | None = None) -> None:
        self.profile = profile
        self.session_manager = session_manager
        self.retry_policy = retry_policy or HttpRetryPolicy()

    @staticmethod
    def _payload(operation: WebOperationProfile, args: Mapping[str, Any]) -> Any:
        if operation.rpc_method is not None:
            if operation.rpc_method == TBD_CURRENT_PRODUCT:
                raise WebProfileUnboundError(f"WEB_OPERATION_NOT_SOURCE_BOUND:{operation.semantic_action}")
            return {"method": operation.rpc_method, "params": dict(args)}
        return dict(args)

    async def _request_operation(self, operation: WebOperationProfile, args: Mapping[str, Any]) -> HttpResponse:
        return await self.session_manager.request(
            HttpRequest(method=operation.method, path=operation.endpoint,
                        json_body=self._payload(operation, args), mutation=operation.mutation),
            retry_policy=self.retry_policy,
        )

    @staticmethod
    def _to_result(response: HttpResponse) -> EntryResult:
        output = response.json_body if response.json_body is not None else response.text
        return EntryResult(
            accepted=response.success, status_code=response.status_code, output=output,
            evidence=(response.evidence,), error=None if response.success else f"WEB_HTTP_{response.status_code}",
        )

    async def _readback_after_unknown(self, operation: WebOperationProfile, args: Mapping[str, Any]) -> EntryResult | None:
        if not operation.readback_operation:
            return None
        readback_op = self.profile.operation(operation.readback_operation)
        if readback_op.mutation:
            raise WebApiProfileError("WEB_READBACK_OPERATION_MUST_BE_READ_ONLY")
        return self._to_result(await self._request_operation(readback_op, args))

    async def execute(self, semantic_action: str, args: Mapping[str, Any], ctx: Any = None) -> EntryResult:
        del ctx
        operation = self.profile.operation(semantic_action)
        try:
            return self._to_result(await self._request_operation(operation, args))
        except HttpMutationResultUnknown as exc:
            readback = await self._readback_after_unknown(operation, args)
            evidence = [exc.evidence]
            if readback is not None:
                evidence.extend(readback.evidence)
            return EntryResult(
                accepted=False, unknown_result=True, evidence=tuple(evidence),
                readback=(readback.output if readback is not None else None),
                error="HTTP_MUTATION_RESULT_UNKNOWN",
            )

    async def configure_voip_account(self, account: VoipAccount, ctx: Any = None) -> EntryResult:
        return await self.execute("voip.account.configure", {"line": account.line, **dict(account.values)}, ctx)

    async def read_voip_account(self, line: int, ctx: Any = None) -> EntryResult:
        return await self.execute("voip.account.read", {"line": line}, ctx)
