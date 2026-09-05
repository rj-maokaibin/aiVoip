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
from app.automation.persistence import NullRuntimeRecorder, RuntimeRecorder
from app.automation.state_machine import AutomationRunState, AutomationStateMachine
from app.infrastructure.action_route import ActionPurpose, RunIntent


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
    """Deterministic V1 runtime. Rule Engine/LLM are intentionally absent.

    Runtime persistence is audit state, never a physical cleanup fence. Recorder
    failures are sanitized, retained, and force an INCONCLUSIVE result, but they
    cannot prevent restore/reverse verification or DeviceAuthority release-last.
    """

    _MAX_PERSISTENCE_ERRORS = 8

    def __init__(
        self,
        *,
        dispatcher: ActionDispatcher,
        events: InMemoryEventBus,
        assertions: AssertionEngine,
        cleanup: PersistedCleanupCoordinator,
        hooks: RuntimeHooks | None = None,
        recorder: RuntimeRecorder | None = None,
    ) -> None:
        self.dispatcher = dispatcher
        self.events = events
        self.assertions = assertions
        self.cleanup = cleanup
        self.hooks = hooks or RuntimeHooks()
        self.recorder = recorder or NullRuntimeRecorder()

    @staticmethod
    def _note_persistence_error(errors: list[str], code: str) -> None:
        if code in errors:
            return
        if len(errors) < AutomationOrchestrator._MAX_PERSISTENCE_ERRORS:
            errors.append(code)

    def _record_state_safe(
        self,
        errors: list[str],
        run_id: str,
        state: AutomationRunState,
    ) -> bool:
        try:
            self.recorder.record_state(run_id, state)
            return True
        except Exception as exc:
            self._note_persistence_error(
                errors,
                f"RUNTIME_RECORDER_STATE_FAILED:{type(exc).__name__}",
            )
            return False

    def _record_step_safe(
        self,
        errors: list[str],
        run_id: str,
        *,
        step_no: int,
        action_id: str,
        route,
        purpose: ActionPurpose,
        status: str,
        output: dict[str, Any] | None = None,
        action_run_id: str | None = None,
    ) -> bool:
        try:
            self.recorder.record_step(
                run_id,
                step_no=step_no,
                action_id=action_id,
                route=route,
                purpose=purpose,
                status=status,
                output=output,
                action_run_id=action_run_id,
            )
            return True
        except Exception as exc:
            self._note_persistence_error(
                errors,
                f"RUNTIME_RECORDER_STEP_FAILED:{type(exc).__name__}",
            )
            return False

    def _record_assertions_safe(
        self,
        errors: list[str],
        run_id: str,
        evaluation: AssertionEvaluation,
    ) -> bool:
        try:
            self.recorder.record_assertions(run_id, evaluation)
            return True
        except Exception as exc:
            self._note_persistence_error(
                errors,
                f"RUNTIME_RECORDER_ASSERTIONS_FAILED:{type(exc).__name__}",
            )
            return False

    def _finish_safe(
        self,
        errors: list[str],
        run_id: str,
        *,
        state: AutomationRunState,
        evaluation: AssertionEvaluation,
    ) -> bool:
        try:
            self.recorder.finish(
                run_id,
                state=state,
                evaluation=evaluation,
            )
            return True
        except Exception as exc:
            self._note_persistence_error(
                errors,
                f"RUNTIME_RECORDER_FINISH_FAILED:{type(exc).__name__}",
            )
            return False

    @staticmethod
    def _persistence_inconclusive(
        evaluation: AssertionEvaluation,
        errors: list[str],
    ) -> AssertionEvaluation:
        if not errors:
            return evaluation
        reason = (
            "RUNTIME_PERSISTENCE_FAILURE:"
            + "|".join(errors)
            + f";PRIOR_VERDICT={evaluation.verdict.value}"
        )
        return AssertionEvaluation(
            verdict=TestVerdict.INCONCLUSIVE,
            results=evaluation.results,
            reason=reason,
        )

    def _transition(
        self,
        machine: AutomationStateMachine,
        run_id: str,
        target: AutomationRunState,
        persistence_errors: list[str],
    ) -> AutomationRunState:
        state = machine.transition(target)
        self._record_state_safe(persistence_errors, run_id, state)
        return state

    def _to_cleanup(
        self,
        machine: AutomationStateMachine,
        run_id: str,
        persistence_errors: list[str],
    ) -> AutomationRunState:
        before = machine.state
        state = machine.to_cleanup()
        if state != before:
            self._record_state_safe(persistence_errors, run_id, state)
        return state

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
        persistence_errors: list[str] = []

        try:
            self._record_state_safe(
                persistence_errors,
                run_id,
                AutomationRunState.CREATED,
            )
            self._transition(
                machine, run_id, AutomationRunState.PRECHECK, persistence_errors
            )
            precheck = await self.hooks.precheck(context)
            if not precheck.ok:
                blocked_reason = precheck.reason or "PRECHECK_FAILED"
                raise RuntimeBlocked(blocked_reason)

            self._transition(
                machine, run_id, AutomationRunState.RESERVE, persistence_errors
            )
            try:
                context.authority_token = await self.hooks.reserve(context)
            except RuntimeBlocked:
                raise
            except Exception as exc:
                code = getattr(exc, "code", type(exc).__name__)
                if str(code) == "LEASE_BUSY":
                    raise RuntimeBlocked("LEASE_BUSY") from exc
                raise

            self._transition(
                machine, run_id, AutomationRunState.SNAPSHOT, persistence_errors
            )
            await self.hooks.snapshot(context)

            self._transition(
                machine, run_id, AutomationRunState.PROVISION, persistence_errors
            )
            await self.hooks.provision(context)

            self._transition(
                machine, run_id, AutomationRunState.ARM, persistence_errors
            )
            await self.hooks.arm(context)

            self._transition(
                machine, run_id, AutomationRunState.EXECUTE, persistence_errors
            )
            dispatch_context = DispatchContext(
                run_id=run_id,
                intent=intent,
                case_entry=case.entry,
                authority_token=context.authority_token,
                parameters=dict(case.parameters),
                metadata=context.metadata,
            )
            for step_no, step in enumerate(case.steps, start=1):
                if isinstance(step, ActionStepSpec):
                    try:
                        dispatched = await self.dispatcher.dispatch(
                            context=dispatch_context,
                            action_id=step.action,
                            purpose=step.purpose,
                            args=step.args,
                        )
                    except ActionDispatchError as exc:
                        self._record_step_safe(
                            persistence_errors,
                            run_id,
                            step_no=step_no,
                            action_id=step.action,
                            route=None,
                            purpose=step.purpose,
                            status="DISPATCH_ERROR",
                            output={"error": str(exc)},
                        )
                        inconclusive_reason = f"ACTION_DISPATCH:{exc}"
                        break
                    status = (
                        "UNKNOWN" if dispatched.result.unknown_result
                        else "SUCCEEDED" if dispatched.result.success
                        else "FAILED"
                    )
                    self._record_step_safe(
                        persistence_errors,
                        run_id,
                        step_no=step_no,
                        action_id=step.action,
                        route=dispatched.route,
                        purpose=step.purpose,
                        status=status,
                        output=dispatched.result.output,
                    )
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
                        self._record_step_safe(
                            persistence_errors,
                            run_id,
                            step_no=step_no,
                            action_id=f"wait_for.{step.event}",
                            route=None,
                            purpose=ActionPurpose.OBSERVATION,
                            status="TIMEOUT",
                            output={"timeout_seconds": step.timeout_seconds},
                        )
                        inconclusive_reason = f"EVENT_TIMEOUT:{step.event}"
                        break
                    self._record_step_safe(
                        persistence_errors,
                        run_id,
                        step_no=step_no,
                        action_id=f"wait_for.{step.event}",
                        route=None,
                        purpose=ActionPurpose.OBSERVATION,
                        status="SUCCEEDED",
                        output={"event": step.event, "payload": event.payload},
                    )
                    context.evidence.put(
                        f"event:{step.event}",
                        EvidenceEnvelope(
                            data=event.payload,
                            evidence_refs=event.evidence_refs,
                            source_timestamp=event.source_timestamp,
                            route=None,
                        ),
                    )

            self._transition(
                machine, run_id, AutomationRunState.ASSERT, persistence_errors
            )
        except RuntimeBlocked as exc:
            blocked_reason = blocked_reason or str(exc)
        except Exception as exc:
            inconclusive_reason = inconclusive_reason or (
                f"RUNTIME_EXCEPTION:{type(exc).__name__}"
            )
        finally:
            self._to_cleanup(machine, run_id, persistence_errors)
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
            finally:
                for code in self.cleanup.persistence_errors:
                    self._note_persistence_error(persistence_errors, code)

            if machine.state == AutomationRunState.CLEANUP:
                self._transition(
                    machine,
                    run_id,
                    AutomationRunState.VERIFY_CLEANUP,
                    persistence_errors,
                )
            if machine.state == AutomationRunState.VERIFY_CLEANUP:
                self._transition(
                    machine,
                    run_id,
                    AutomationRunState.REPORT,
                    persistence_errors,
                )

        evaluation = self.assertions.evaluate(
            case.assertions,
            context.evidence,
            parameters=case.parameters,
            blocked_reason=blocked_reason,
            inconclusive_reason=inconclusive_reason,
            cleanup_verified=cleanup_verified,
        )
        self._record_assertions_safe(persistence_errors, run_id, evaluation)
        evaluation = self._persistence_inconclusive(evaluation, persistence_errors)

        await self.hooks.report(context, evaluation)

        terminal = {
            TestVerdict.PASS: AutomationRunState.PASSED,
            TestVerdict.FAIL: AutomationRunState.FAILED,
            TestVerdict.BLOCKED: AutomationRunState.BLOCKED,
            TestVerdict.INCONCLUSIVE: AutomationRunState.INCONCLUSIVE,
        }[evaluation.verdict]

        if not self._finish_safe(
            persistence_errors,
            run_id,
            state=terminal,
            evaluation=evaluation,
        ):
            evaluation = self._persistence_inconclusive(evaluation, persistence_errors)
            terminal = AutomationRunState.INCONCLUSIVE

        machine.finish(terminal)
        return OrchestrationResult(
            state=machine.state,
            verdict=evaluation.verdict,
            assertions=evaluation,
            state_history=tuple(machine.history),
            terminal_reason=evaluation.reason,
        )
