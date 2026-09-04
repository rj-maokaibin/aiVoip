from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable

from app.infrastructure.action_route import (
    ActionEntry,
    ActionPurpose,
    ActionRoute,
    ActionTransport,
    RunIntent,
)


class ActionDispatchError(RuntimeError):
    pass


@dataclass(frozen=True)
class ActionEvidence:
    source: str
    data: Any
    evidence_refs: tuple[str, ...] = ()
    source_timestamp: datetime | None = None


@dataclass(frozen=True)
class ActionHandlerResult:
    success: bool
    output: dict[str, Any] = field(default_factory=dict)
    evidence: tuple[ActionEvidence, ...] = ()
    unknown_result: bool = False


@dataclass
class DispatchContext:
    run_id: str
    intent: RunIntent
    case_entry: ActionEntry
    authority_token: Any = None
    parameters: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


ActionHandler = Callable[
    [DispatchContext, dict[str, Any]],
    Awaitable[ActionHandlerResult],
]


@dataclass(frozen=True)
class ActionBinding:
    action_id: str
    route: ActionRoute
    handler: ActionHandler
    mutates: bool = False


@dataclass(frozen=True)
class ActionDispatchResult:
    action_id: str
    route: ActionRoute
    result: ActionHandlerResult


class ActionDispatcher:
    """Resolve one explicit semantic route. There is deliberately no fallback chain."""

    def __init__(self) -> None:
        self._bindings: dict[tuple[str, ActionPurpose, ActionEntry], ActionBinding] = {}

    def register(
        self,
        *,
        action_id: str,
        route: ActionRoute,
        handler: ActionHandler,
        mutates: bool = False,
    ) -> None:
        key = (action_id, route.purpose, route.entry)
        if key in self._bindings:
            raise ActionDispatchError(
                f"DUPLICATE_ACTION_BINDING:{action_id}:{route.purpose.value}:{route.entry.value}"
            )
        if route.entry == ActionEntry.WEB and route.transport != ActionTransport.HTTP_API:
            raise ActionDispatchError("WEB_ENTRY_REQUIRES_HTTP_API")
        if route.transport == ActionTransport.SSH and route.entry != ActionEntry.NONE:
            raise ActionDispatchError("SSH_TRANSPORT_REQUIRES_ENTRY_NONE")
        self._bindings[key] = ActionBinding(
            action_id=action_id,
            route=route,
            handler=handler,
            mutates=mutates,
        )

    def _resolve(
        self,
        *,
        action_id: str,
        purpose: ActionPurpose,
        case_entry: ActionEntry,
    ) -> ActionBinding:
        exact = self._bindings.get((action_id, purpose, case_entry))
        if exact is not None:
            return exact
        # Observation/evidence/setup/cleanup may intentionally use shared entry=none.
        # A test step never falls back from its declared product entry to SSH/none.
        if purpose != ActionPurpose.TEST:
            neutral = self._bindings.get((action_id, purpose, ActionEntry.NONE))
            if neutral is not None:
                return neutral
        raise ActionDispatchError(
            f"ACTION_ROUTE_NOT_FOUND:{action_id}:{purpose.value}:{case_entry.value}"
        )

    async def dispatch(
        self,
        *,
        context: DispatchContext,
        action_id: str,
        purpose: ActionPurpose,
        args: dict[str, Any] | None = None,
    ) -> ActionDispatchResult:
        binding = self._resolve(
            action_id=action_id,
            purpose=purpose,
            case_entry=context.case_entry,
        )
        route = binding.route
        if binding.mutates and context.authority_token is None:
            raise ActionDispatchError("MUTATION_AUTHORITY_REQUIRED")
        if purpose == ActionPurpose.TEST and context.case_entry != ActionEntry.NONE:
            if route.entry != context.case_entry:
                raise ActionDispatchError("TEST_PATH_ENTRY_MISMATCH")
            if context.case_entry == ActionEntry.WEB and route.transport != ActionTransport.HTTP_API:
                raise ActionDispatchError("WEB_TEST_PATH_MUST_USE_HTTP_API")
        if (
            purpose == ActionPurpose.TEST
            and context.case_entry == ActionEntry.WEB
            and binding.mutates
            and route.transport == ActionTransport.SSH
        ):
            raise ActionDispatchError("WEB_TEST_SSH_FALLBACK_FORBIDDEN")
        # Exactly one handler invocation. A failed/unknown handler result is returned
        # to the Assertion Engine; Dispatcher never tries a second route.
        result = await binding.handler(context, dict(args or {}))
        return ActionDispatchResult(action_id=action_id, route=route, result=result)
