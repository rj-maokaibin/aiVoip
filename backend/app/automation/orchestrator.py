from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from app.automation.actions.dispatcher import (
    ActionDispatchError,
    ActionDispatcher,
    DispatchContext,
)
from app.automation.assertions.engine import AssertionEngine, AssertionEvaluation
from app.automation.assertions.resolver import EvidenceEnvelope, NormalizedEvidenceStore
from app.automation.cleanup import AutomationCleanupError, PersistedCleanupCoordinator
from app.automation.contracts import ActionStepSpec, TestCaseSpec, TestVerdict, WaitForSpec
from app.automation.event_wait import EventWaitTimeout, InMemoryEventBus
from app.automation.state_machine import AutomationRunState, AutomationStateMachine
from app.infrastructure.action_route import RunIntent


class RuntimeBlocked(RuntimeError):
    pass


@dataclass(frozen=True)
class PrecheckResult:
    ok: bool
    reason: str | None = None


@dataclass
class AutomationRunContext:
    run_id: str
    case: TestCaseSpec
    worker_id: str
    intent: RunIntent = RunIntent.VERIFY
    authority_token: Any = None
    evidence: NormalizedEvidenceStore = field(default_factory=NormalizedEvidenceStore)
    metadata: dict[str, Any] = field(default_factory=dict)


AsyncHook = Callable[[AutomationRunContext], Awaitable[Any]]
PrecheckHook = Callable[[AutomationRunContext], Awaitable[PrecheckResult]]
ReportHook = Callable[[AutomationRunContext, AssertionEvaluation], Awaitable[Any]]


async def _default_precheck(_: AutomationRunContext) -> PrecheckResult:
    return PrecheckResult(True)


async def _noop(_: AutomationRunContext) -> None:
    return None


async def _default_reserve(_: AutomationRunContext) -> Any:
    return None


async def _default_report(_: AutomationRunContext, __: AssertionEvaluation) -> None:
    return None


@dataclass
class RuntimeHooks:
    precheck: PrecheckHook = _default_precheck
    reserve: AsyncHook = _default_reserve
    snapshot: AsyncHook = _noop
    provision: AsyncHook = _noop
    arm: AsyncHook = _noop
    report: ReportHook = _default_report


@dataclass(frozen=True)
class OrchestrationResult:
    state: AutomationRunState
    verdict: TestVerdict
    assertions: AssertionEvaluation
    state_history: tuple[AutomationRunState, ...]
    terminal_reason: str | None


class AutomationOrchestrator:
    """Deterministic V1 runtime. Rule Engine/LLM are intentionally absent."""

    def __init__(
        self,
        *,
        dispatcher: ActionDispatcher,
        events: InMemoryEventBus,
        assertions: AssertionEngine,
        cleanup: PersistedCleanupCoordinator,
        hooks: RuntimeHooks | None = None,
    ) -> None:
        self.dispatcher = dispatcher
        self.events = events
        self.assertions = assertions
        self.cleanup = cleanup
        self.hooks = hooks or RuntimeHooks()

    async def run(
        self,
        case: TestCaseSpec,
        *,
        run_id: str,
        worker_id: str,
        intent: RunIntent = RunIntent.VERIFY,
    ) -> OrchestrationResult:
        machine = AutomationStateMachine()
        context = AutomationRunContext(
            run_id=run_id,
            case=case,
            worker_id=worker_id,
            intent=intent,
        )
        blocked_reason: str | None = None
        inconclusive_reason: str | None = None
        cleanup_verified = False

        try:
            machine.transition(AutomationRunState.PRECHECK)
            precheck = await self.hooks.precheck(context)
            if not precheck.ok:
                blocked_reason = precheck.reason or "PRECHECK_FAILED"
                raise RuntimeBlocked(blocked_reason)

            machine.transition(AutomationRunState.RESERVE)
            try:
                context.authority_token = await self.hooks.reserve(context)
            except RuntimeBlocked:
                raise
            except Exception as exc:
                code = getattr(exc, "code", type(exc).__name__)
                if str(code) == "LEASE_BUSY":
                    raise RuntimeBlocked("LEASE_BUSY") from exc
                raise

            machine.transition(AutomationRunState.SNAPSHOT)
            await self.hooks.snapshot(context)

            machine.transition(AutomationRunState.PROVISION)
            await self.hooks.provision(context)

            machine.transition(AutomationRunState.ARM)
            await self.hooks.arm(context)

            machine.transition(AutomationRunState.EXECUTE)
            dispatch_context = DispatchContext(
                run_id=run_id,
                intent=intent,
                case_entry=case.entry,
                authority_token=context.authority_token,
                parameters=dict(case.parameters),
                metadata=context.metadata,
            )
            for step in case.steps:
                if isinstance(step, ActionStepSpec):
                    try:
                        dispatched = await self.dispatcher.dispatch(
                            context=dispatch_context,
                            action_id=step.action,
                            purpose=step.purpose,
                            args=step.args,
                        )
                    except ActionDispatchError as exc:
                        inconclusive_reason = f"ACTION_DISPATCH:{exc}"
                        break
                    for item in dispatched.result.evidence:
                        context.evidence.put(
                            item.source,
                            EvidenceEnvelope(
                                data=item.data,
                                evidence_refs=item.evidence_refs,
                                source_timestamp=item.source_timestamp,
                                route=dispatched.route.as_dict(),
                            ),
                        )
                    if dispatched.result.unknown_result:
                        inconclusive_reason = f"ACTION_RESULT_UNKNOWN:{step.action}"
                        break
                elif isinstance(step, WaitForSpec):
                    try:
                        event = await self.events.wait_for(
                            step.event,
                            timeout=step.timeout_seconds,
                        )
                    except EventWaitTimeout:
                        inconclusive_reason = f"EVENT_TIMEOUT:{step.event}"
                        break
                    context.evidence.put(
                        f"event:{step.event}",
                        EvidenceEnvelope(
                            data=event.payload,
                            evidence_refs=event.evidence_refs,
                            source_timestamp=event.source_timestamp,
                            route=None,
                        ),
                    )

            machine.transition(AutomationRunState.ASSERT)
        except RuntimeBlocked as exc:
            blocked_reason = blocked_reason or str(exc)
        except Exception as exc:
            inconclusive_reason = inconclusive_reason or (
                f"RUNTIME_EXCEPTION:{type(exc).__name__}"
            )
        finally:
            machine.to_cleanup()
            try:
                await self.cleanup.run(run_id=run_id)
                cleanup_verified = True
            except AutomationCleanupError as exc:
                inconclusive_reason = inconclusive_reason or str(exc)
                cleanup_verified = False
            except Exception as exc:
                inconclusive_reason = inconclusive_reason or (
                    f"CLEANUP_EXCEPTION:{type(exc).__name__}"
                )
                cleanup_verified = False

            if machine.state == AutomationRunState.CLEANUP:
                machine.transition(AutomationRunState.VERIFY_CLEANUP)
            if machine.state == AutomationRunState.VERIFY_CLEANUP:
                machine.transition(AutomationRunState.REPORT)

        evaluation = self.assertions.evaluate(
            case.assertions,
            context.evidence,
            parameters=case.parameters,
            blocked_reason=blocked_reason,
            inconclusive_reason=inconclusive_reason,
            cleanup_verified=cleanup_verified,
        )
        await self.hooks.report(context, evaluation)

        terminal = {
            TestVerdict.PASS: AutomationRunState.PASSED,
            TestVerdict.FAIL: AutomationRunState.FAILED,
            TestVerdict.BLOCKED: AutomationRunState.BLOCKED,
            TestVerdict.INCONCLUSIVE: AutomationRunState.INCONCLUSIVE,
        }[evaluation.verdict]
        machine.finish(terminal)
        return OrchestrationResult(
            state=machine.state,
            verdict=evaluation.verdict,
            assertions=evaluation,
            state_history=tuple(machine.history),
            terminal_reason=evaluation.reason,
        )
