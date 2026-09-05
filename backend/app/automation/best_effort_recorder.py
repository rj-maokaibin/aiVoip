from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.automation.assertions.engine import AssertionEvaluation
from app.automation.persistence import RuntimeRecorder
from app.automation.state_machine import AutomationRunState
from app.infrastructure.action_route import ActionPurpose, ActionRoute


@dataclass(frozen=True)
class RuntimeRecorderFailure:
    """Sanitized persistence diagnostic; never carries exception text or secrets."""

    run_id: str
    operation: str
    error_type: str


@dataclass
class BestEffortRuntimeRecorder:
    """Prevent persistence faults from changing test execution or cleanup semantics.

    The wrapped recorder remains the authoritative persistence implementation.
    This adapter only isolates its failures and exposes sanitized diagnostics so a
    database/runtime persistence outage cannot turn into an automation verdict or
    prevent cleanup from running.
    """

    inner: RuntimeRecorder
    failures: list[RuntimeRecorderFailure] = field(default_factory=list)

    def _call(self, run_id: str, operation: str, func, /, *args, **kwargs) -> None:
        try:
            func(*args, **kwargs)
        except Exception as exc:
            self.failures.append(
                RuntimeRecorderFailure(
                    run_id=run_id,
                    operation=operation,
                    error_type=type(exc).__name__,
                )
            )

    def failures_for(self, run_id: str) -> tuple[RuntimeRecorderFailure, ...]:
        return tuple(item for item in self.failures if item.run_id == run_id)

    def record_state(self, run_id: str, state: AutomationRunState) -> None:
        self._call(run_id, "record_state", self.inner.record_state, run_id, state)

    def record_step(
        self,
        run_id: str,
        *,
        step_no: int,
        action_id: str,
        route: ActionRoute | None,
        purpose: ActionPurpose,
        status: str,
        output: dict[str, Any] | None = None,
        action_run_id: str | None = None,
    ) -> None:
        self._call(
            run_id,
            "record_step",
            self.inner.record_step,
            run_id,
            step_no=step_no,
            action_id=action_id,
            route=route,
            purpose=purpose,
            status=status,
            output=output,
            action_run_id=action_run_id,
        )

    def record_assertions(self, run_id: str, evaluation: AssertionEvaluation) -> None:
        self._call(
            run_id,
            "record_assertions",
            self.inner.record_assertions,
            run_id,
            evaluation,
        )

    def finish(
        self,
        run_id: str,
        *,
        state: AutomationRunState,
        evaluation: AssertionEvaluation,
    ) -> None:
        self._call(
            run_id,
            "finish",
            self.inner.finish,
            run_id,
            state=state,
            evaluation=evaluation,
        )
