from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from sqlalchemy import delete, select

from app.automation.assertions.engine import AssertionEvaluation
from app.automation.models import AssertionResult, AutomationTestRun, AutomationTestStepRun
from app.automation.state_machine import AutomationRunState
from app.core.ids import new_id
from app.infrastructure.action_route import ActionPurpose, ActionRoute


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RuntimeRecorder(Protocol):
    """Persistence boundary for Test Runtime facts; no Evidence/Lease replacement."""

    def record_state(self, run_id: str, state: AutomationRunState) -> None: ...

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
    ) -> None: ...

    def record_assertions(
        self,
        run_id: str,
        evaluation: AssertionEvaluation,
    ) -> None: ...

    def finish(
        self,
        run_id: str,
        *,
        state: AutomationRunState,
        evaluation: AssertionEvaluation,
    ) -> None: ...


class NullRuntimeRecorder:
    def record_state(self, run_id: str, state: AutomationRunState) -> None:
        return None

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
        return None

    def record_assertions(
        self,
        run_id: str,
        evaluation: AssertionEvaluation,
    ) -> None:
        return None

    def finish(
        self,
        run_id: str,
        *,
        state: AutomationRunState,
        evaluation: AssertionEvaluation,
    ) -> None:
        return None


@dataclass
class InMemoryRuntimeRecorder:
    states: list[tuple[str, str]] = field(default_factory=list)
    steps: list[dict[str, Any]] = field(default_factory=list)
    assertions: list[dict[str, Any]] = field(default_factory=list)
    finals: list[dict[str, Any]] = field(default_factory=list)

    def record_state(self, run_id: str, state: AutomationRunState) -> None:
        self.states.append((run_id, state.value))

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
        self.steps.append({
            "run_id": run_id,
            "step_no": step_no,
            "action_id": action_id,
            "route": route.as_dict() if route is not None else None,
            "purpose": purpose.value,
            "status": status,
            "output": dict(output or {}),
            "action_run_id": action_run_id,
        })

    def record_assertions(
        self,
        run_id: str,
        evaluation: AssertionEvaluation,
    ) -> None:
        self.assertions = [
            {
                "run_id": run_id,
                "assertion_no": index,
                "assertion_id": item.assertion_id,
                "source": item.source,
                "path": item.path,
                "operator": item.operator,
                "expected": item.expected,
                "actual": item.actual,
                "verdict": item.verdict.value,
                "evidence_refs": list(item.evidence_refs),
                "route": item.route,
                "source_timestamp": item.source_timestamp,
                "reason": item.reason,
            }
            for index, item in enumerate(evaluation.results, start=1)
        ]

    def finish(
        self,
        run_id: str,
        *,
        state: AutomationRunState,
        evaluation: AssertionEvaluation,
    ) -> None:
        self.finals.append({
            "run_id": run_id,
            "state": state.value,
            "verdict": evaluation.verdict.value,
            "reason": evaluation.reason,
        })


class SqlAlchemyRuntimeRecorder:
    """Persists run/step/assertion facts into the V1 Test Domain tables."""

    def __init__(self, session_factory) -> None:
        self.session_factory = session_factory

    def _run(self, db, run_id: str) -> AutomationTestRun:
        row = db.get(AutomationTestRun, run_id)
        if row is None:
            raise RuntimeError(f"AUTOMATION_TEST_RUN_NOT_FOUND:{run_id}")
        return row

    def record_state(self, run_id: str, state: AutomationRunState) -> None:
        with self.session_factory() as db:
            with db.begin():
                row = self._run(db, run_id)
                row.status = state.value
                row.updated_at = utcnow()
                if state == AutomationRunState.PRECHECK and row.started_at is None:
                    row.started_at = utcnow()

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
        with self.session_factory() as db:
            with db.begin():
                row = db.scalar(select(AutomationTestStepRun).where(
                    AutomationTestStepRun.test_run_id == run_id,
                    AutomationTestStepRun.step_no == step_no,
                ))
                if row is None:
                    row = AutomationTestStepRun(
                        id=new_id(),
                        test_run_id=run_id,
                        step_no=step_no,
                        action_id=action_id,
                        route_json=route.as_dict() if route is not None else None,
                        purpose=purpose.value,
                        status=status,
                        action_run_id=action_run_id,
                        output_json=dict(output or {}),
                        started_at=utcnow(),
                        finished_at=utcnow(),
                    )
                    db.add(row)
                else:
                    row.action_id = action_id
                    row.route_json = route.as_dict() if route is not None else None
                    row.purpose = purpose.value
                    row.status = status
                    row.action_run_id = action_run_id
                    row.output_json = dict(output or {})
                    row.finished_at = utcnow()

    def record_assertions(
        self,
        run_id: str,
        evaluation: AssertionEvaluation,
    ) -> None:
        with self.session_factory() as db:
            with db.begin():
                db.execute(delete(AssertionResult).where(
                    AssertionResult.test_run_id == run_id
                ))
                for index, item in enumerate(evaluation.results, start=1):
                    db.add(AssertionResult(
                        id=new_id(),
                        test_run_id=run_id,
                        assertion_no=index,
                        assertion_id=item.assertion_id,
                        source=item.source,
                        path=item.path,
                        operator=item.operator,
                        expected_json=item.expected,
                        actual_json=item.actual,
                        verdict=item.verdict.value,
                        evidence_refs_json=list(item.evidence_refs),
                        route_json=dict(item.route) if item.route else None,
                        source_timestamp=item.source_timestamp,
                        reason=item.reason,
                    ))

    def finish(
        self,
        run_id: str,
        *,
        state: AutomationRunState,
        evaluation: AssertionEvaluation,
    ) -> None:
        with self.session_factory() as db:
            with db.begin():
                row = self._run(db, run_id)
                row.status = state.value
                row.verdict = evaluation.verdict.value
                row.terminal_reason = evaluation.reason
                row.finished_at = utcnow()
                row.updated_at = utcnow()
