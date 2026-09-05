from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol

from sqlalchemy import select

from app.automation.models import AutomationTestStepRun
from app.core.ids import new_id
from app.infrastructure.action_route import ActionPurpose


class AutomationCleanupError(RuntimeError):
    pass


CleanupAction = Callable[[], Awaitable[dict[str, Any] | None]]
CleanupVerify = Callable[[], Awaitable[bool | tuple[bool, dict[str, Any]]]]


@dataclass(frozen=True)
class CleanupStepSpec:
    name: str
    action: CleanupAction
    verify: CleanupVerify
    release_authority: bool = False


class CleanupStepStore(Protocol):
    def verified(self, run_id: str) -> set[str]: ...
    def record(
        self,
        run_id: str,
        *,
        ordinal: int,
        step_name: str,
        status: str,
        details: dict[str, Any],
    ) -> None: ...


@dataclass
class InMemoryCleanupStepStore:
    rows: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)

    def verified(self, run_id: str) -> set[str]:
        return {
            step for (stored_run, step), row in self.rows.items()
            if stored_run == run_id and row.get("status") == "VERIFIED"
        }

    def record(
        self,
        run_id: str,
        *,
        ordinal: int,
        step_name: str,
        status: str,
        details: dict[str, Any],
    ) -> None:
        self.rows[(run_id, step_name)] = {
            "ordinal": ordinal,
            "status": status,
            "details": dict(details),
        }


class SqlAlchemyCleanupStepStore:
    """Persists cleanup progress in AutomationTestStepRun; no parallel Evidence store."""

    def __init__(self, session_factory) -> None:
        self.session_factory = session_factory

    def verified(self, run_id: str) -> set[str]:
        with self.session_factory() as db:
            rows = list(db.scalars(select(AutomationTestStepRun).where(
                AutomationTestStepRun.test_run_id == run_id,
                AutomationTestStepRun.purpose == ActionPurpose.CLEANUP.value,
                AutomationTestStepRun.status == "VERIFIED",
            )))
            return {row.action_id.removeprefix("cleanup.") for row in rows}

    def record(
        self,
        run_id: str,
        *,
        ordinal: int,
        step_name: str,
        status: str,
        details: dict[str, Any],
    ) -> None:
        action_id = f"cleanup.{step_name}"
        with self.session_factory() as db:
            with db.begin():
                row = db.scalar(select(AutomationTestStepRun).where(
                    AutomationTestStepRun.test_run_id == run_id,
                    AutomationTestStepRun.action_id == action_id,
                    AutomationTestStepRun.purpose == ActionPurpose.CLEANUP.value,
                ))
                if row is None:
                    row = AutomationTestStepRun(
                        id=new_id(),
                        test_run_id=run_id,
                        step_no=100000 + ordinal,
                        action_id=action_id,
                        route_json=None,
                        purpose=ActionPurpose.CLEANUP.value,
                        status=status,
                        output_json=dict(details),
                    )
                    db.add(row)
                else:
                    row.status = status
                    row.output_json = dict(details)


@dataclass(frozen=True)
class CleanupExecution:
    name: str
    verified: bool
    details: dict[str, Any]


class PersistedCleanupCoordinator:
    """Crash-resumable cleanup with reverse verification and authority release last.

    Cleanup persistence is audit/recovery metadata, not a physical cleanup fence.
    A store read/write failure is retained as a sanitized persistence error for the
    orchestrator to downgrade the run, but it must never prevent a verified restore
    or the final DeviceAuthority release from being attempted in the same run.
    """

    _MAX_PERSISTENCE_ERRORS = 8

    def __init__(
        self,
        *,
        store: CleanupStepStore,
        steps: tuple[CleanupStepSpec, ...],
    ) -> None:
        if not steps:
            raise ValueError("CLEANUP_STEPS_REQUIRED")
        release_indexes = [i for i, step in enumerate(steps) if step.release_authority]
        if release_indexes != [len(steps) - 1]:
            raise ValueError("AUTHORITY_RELEASE_MUST_BE_LAST")
        self.store = store
        self.steps = steps
        self.persistence_errors: list[str] = []

    def _note_persistence_error(self, code: str) -> None:
        if code in self.persistence_errors:
            return
        if len(self.persistence_errors) < self._MAX_PERSISTENCE_ERRORS:
            self.persistence_errors.append(code)

    def _record_safe(
        self,
        run_id: str,
        *,
        ordinal: int,
        step_name: str,
        status: str,
        details: dict[str, Any],
    ) -> None:
        try:
            self.store.record(
                run_id,
                ordinal=ordinal,
                step_name=step_name,
                status=status,
                details=details,
            )
        except Exception as exc:
            self._note_persistence_error(
                f"CLEANUP_STORE_RECORD_FAILED:{type(exc).__name__}"
            )

    async def run(self, *, run_id: str) -> tuple[CleanupExecution, ...]:
        self.persistence_errors = []
        try:
            verified = self.store.verified(run_id)
        except Exception as exc:
            self._note_persistence_error(
                f"CLEANUP_STORE_READ_FAILED:{type(exc).__name__}"
            )
            # Physical cleanup remains authoritative. Cleanup actions are required
            # to observe current state and reverse-verify; persistence failure alone
            # cannot justify skipping restore or authority release.
            verified = set()

        executions: list[CleanupExecution] = []
        for ordinal, step in enumerate(self.steps, start=1):
            if step.name in verified:
                executions.append(CleanupExecution(
                    step.name, True, {"idempotent_replay": True}
                ))
                continue
            try:
                action_details = dict(await step.action() or {})
                observed = await step.verify()
                if isinstance(observed, tuple):
                    ok, verify_details = observed
                    verify_details = dict(verify_details)
                else:
                    ok, verify_details = bool(observed), {}
                details = {**action_details, **verify_details}
            except Exception as exc:
                details = {"exception": type(exc).__name__}
                self._record_safe(
                    run_id,
                    ordinal=ordinal,
                    step_name=step.name,
                    status="FAILED",
                    details=details,
                )
                raise AutomationCleanupError(
                    f"CLEANUP_STEP_EXCEPTION:{step.name}:{type(exc).__name__}"
                ) from exc
            if not ok:
                self._record_safe(
                    run_id,
                    ordinal=ordinal,
                    step_name=step.name,
                    status="FAILED",
                    details=details,
                )
                raise AutomationCleanupError(
                    f"CLEANUP_REVERSE_VERIFY_FAILED:{step.name}"
                )
            self._record_safe(
                run_id,
                ordinal=ordinal,
                step_name=step.name,
                status="VERIFIED",
                details=details,
            )
            verified.add(step.name)
            executions.append(CleanupExecution(step.name, True, details))
        return tuple(executions)
