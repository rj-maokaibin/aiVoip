from __future__ import annotations

import pytest

from app.automation.actions.dispatcher import ActionDispatcher, ActionHandlerResult
from app.automation.assertions.engine import AssertionEngine
from app.automation.cleanup import CleanupStepSpec, PersistedCleanupCoordinator
from app.automation.contracts import (
    ActionStepSpec,
    CleanupSpec,
    TestCaseSpec,
    TestContractStatus,
    TestVerdict,
)
from app.automation.event_wait import InMemoryEventBus
from app.automation.orchestrator import AutomationOrchestrator
from app.automation.persistence import NullRuntimeRecorder
from app.automation.state_machine import AutomationRunState
from app.infrastructure.action_route import (
    ActionBackend,
    ActionEntry,
    ActionPurpose,
    ActionRoute,
    ActionTransport,
)


class _FailingRuntimeRecorder(NullRuntimeRecorder):
    def __init__(self, *, fail_state: bool = False, fail_step: bool = False, fail_finish: bool = False) -> None:
        self.fail_state = fail_state
        self.fail_step = fail_step
        self.fail_finish = fail_finish

    def record_state(self, run_id, state):
        del run_id
        if self.fail_state and state is AutomationRunState.CLEANUP:
            raise RuntimeError("db password=SUPER_SECRET must never escape")

    def record_step(self, run_id, **kwargs):
        del run_id, kwargs
        if self.fail_step:
            raise RuntimeError("db password=SUPER_SECRET must never escape")

    def finish(self, run_id, *, state, evaluation):
        del run_id, state, evaluation
        if self.fail_finish:
            raise RuntimeError("db password=SUPER_SECRET must never escape")


class _FailingCleanupStore:
    def verified(self, run_id: str) -> set[str]:
        del run_id
        return set()

    def record(self, run_id: str, **kwargs) -> None:
        del run_id, kwargs
        raise RuntimeError("postgres://user:SUPER_SECRET@db must never escape")


class _FailingCleanupReadStore(_FailingCleanupStore):
    def verified(self, run_id: str) -> set[str]:
        del run_id
        raise RuntimeError("postgres://user:SUPER_SECRET@db must never escape")


def _case(*, with_step: bool = True) -> TestCaseSpec:
    steps = (
        ActionStepSpec(
            action="test.observe",
            purpose=ActionPurpose.OBSERVATION,
            args={},
        ),
    ) if with_step else ()
    return TestCaseSpec(
        case_id="Persistence-Cleanup-Safety-001",
        version=1,
        name="persistence cleanup safety",
        suite_id="runtime-safety",
        entry=ActionEntry.NONE,
        contract_status=TestContractStatus.ACTIVE,
        environment_profile="unit",
        parameters={},
        snapshot=(),
        steps=steps,
        assertions=(),
        cleanup=CleanupSpec(strategy="restore_snapshot", verify=True),
    )


def _dispatcher() -> ActionDispatcher:
    dispatcher = ActionDispatcher()

    async def observe(_ctx, _args):
        return ActionHandlerResult(success=True, output={"observed": True})

    dispatcher.register(
        action_id="test.observe",
        route=ActionRoute(
            entry=ActionEntry.NONE,
            transport=ActionTransport.SSH,
            backend=ActionBackend.NATIVE_LINUX,
            purpose=ActionPurpose.OBSERVATION,
            target="unit-fake",
        ),
        handler=observe,
        mutates=False,
    )
    return dispatcher


def _cleanup(store, calls: list[str]) -> PersistedCleanupCoordinator:
    async def restore_action():
        calls.append("restore")
        return {"restore_attempted": True}

    async def restore_verify():
        calls.append("restore_verify")
        return True, {"restore_verified": True}

    async def release_action():
        calls.append("release")
        return {"release_attempted": True}

    async def release_verify():
        calls.append("release_verify")
        return True, {"release_verified": True}

    return PersistedCleanupCoordinator(
        store=store,
        steps=(
            CleanupStepSpec("restore", restore_action, restore_verify),
            CleanupStepSpec(
                "release_device_authority",
                release_action,
                release_verify,
                release_authority=True,
            ),
        ),
    )


@pytest.mark.asyncio
async def test_runtime_recorder_state_and_step_failures_do_not_block_cleanup_or_release_last() -> None:
    calls: list[str] = []
    cleanup = _cleanup(
        # Import locally to keep the test focused on the production coordinator.
        __import__("app.automation.cleanup", fromlist=["InMemoryCleanupStepStore"]).InMemoryCleanupStepStore(),
        calls,
    )
    runtime = AutomationOrchestrator(
        dispatcher=_dispatcher(),
        events=InMemoryEventBus(),
        assertions=AssertionEngine(),
        cleanup=cleanup,
        recorder=_FailingRuntimeRecorder(fail_state=True, fail_step=True),
    )

    result = await runtime.run(_case(), run_id="run-recorder-failure", worker_id="worker-1")

    assert calls == ["restore", "restore_verify", "release", "release_verify"]
    assert result.state is AutomationRunState.INCONCLUSIVE
    assert result.verdict is TestVerdict.INCONCLUSIVE
    assert "RUNTIME_PERSISTENCE_FAILURE" in (result.terminal_reason or "")
    assert "RUNTIME_RECORDER_STATE_FAILED:RuntimeError" in (result.terminal_reason or "")
    assert "RUNTIME_RECORDER_STEP_FAILED:RuntimeError" in (result.terminal_reason or "")
    assert "SUPER_SECRET" not in (result.terminal_reason or "")


@pytest.mark.asyncio
@pytest.mark.parametrize("store_cls", [_FailingCleanupStore, _FailingCleanupReadStore])
async def test_cleanup_store_failure_never_prevents_verified_release_last(store_cls) -> None:
    calls: list[str] = []
    cleanup = _cleanup(store_cls(), calls)
    runtime = AutomationOrchestrator(
        dispatcher=_dispatcher(),
        events=InMemoryEventBus(),
        assertions=AssertionEngine(),
        cleanup=cleanup,
        recorder=NullRuntimeRecorder(),
    )

    result = await runtime.run(
        _case(with_step=False),
        run_id="run-cleanup-store-failure",
        worker_id="worker-1",
    )

    assert calls == ["restore", "restore_verify", "release", "release_verify"]
    assert result.state is AutomationRunState.INCONCLUSIVE
    assert result.verdict is TestVerdict.INCONCLUSIVE
    assert "RUNTIME_PERSISTENCE_FAILURE" in (result.terminal_reason or "")
    assert any(code.startswith("CLEANUP_STORE_") for code in cleanup.persistence_errors)
    assert "SUPER_SECRET" not in (result.terminal_reason or "")


@pytest.mark.asyncio
async def test_final_runtime_persistence_failure_cannot_return_pass() -> None:
    calls: list[str] = []
    cleanup = _cleanup(
        __import__("app.automation.cleanup", fromlist=["InMemoryCleanupStepStore"]).InMemoryCleanupStepStore(),
        calls,
    )
    runtime = AutomationOrchestrator(
        dispatcher=_dispatcher(),
        events=InMemoryEventBus(),
        assertions=AssertionEngine(),
        cleanup=cleanup,
        recorder=_FailingRuntimeRecorder(fail_finish=True),
    )

    result = await runtime.run(
        _case(with_step=False),
        run_id="run-finish-failure",
        worker_id="worker-1",
    )

    assert calls[-2:] == ["release", "release_verify"]
    assert result.state is AutomationRunState.INCONCLUSIVE
    assert result.verdict is TestVerdict.INCONCLUSIVE
    assert "RUNTIME_RECORDER_FINISH_FAILED:RuntimeError" in (result.terminal_reason or "")
    assert "SUPER_SECRET" not in (result.terminal_reason or "")
