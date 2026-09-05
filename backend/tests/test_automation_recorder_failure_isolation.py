from __future__ import annotations

import asyncio

from app.automation.actions.dispatcher import ActionDispatcher, ActionEvidence, ActionHandlerResult
from app.automation.assertions.engine import AssertionEngine
from app.automation.cleanup import CleanupStepSpec, InMemoryCleanupStepStore, PersistedCleanupCoordinator
from app.automation.contracts import (
    ActionStepSpec,
    AssertionSpec,
    CleanupSpec,
    TestCaseSpec,
    TestContractStatus,
    TestVerdict,
)
from app.automation.event_wait import InMemoryEventBus
from app.automation.orchestrator import AutomationOrchestrator
from app.automation.state_machine import AutomationRunState
from app.infrastructure.action_route import (
    ActionBackend,
    ActionEntry,
    ActionPurpose,
    ActionRoute,
    ActionTransport,
)


class _FailingRuntimeRecorder:
    def _fail(self) -> None:
        raise RuntimeError("sensitive-recorder-detail-must-not-escape")

    def record_state(self, run_id, state) -> None:
        self._fail()

    def record_step(self, run_id, **kwargs) -> None:
        self._fail()

    def record_assertions(self, run_id, evaluation) -> None:
        self._fail()

    def finish(self, run_id, *, state, evaluation) -> None:
        self._fail()


def test_recorder_failures_do_not_change_assertion_verdict_or_prevent_cleanup() -> None:
    cleanup_calls: list[str] = []

    async def action(_context, _args):
        return ActionHandlerResult(
            success=True,
            output={"accepted": True},
            evidence=(ActionEvidence(source="entry", data={"ok": True}),),
        )

    async def cleanup_action():
        cleanup_calls.append("release")
        return {"released": True}

    async def cleanup_verify():
        return True, {"release_verified": True}

    route = ActionRoute(
        entry=ActionEntry.WEB,
        transport=ActionTransport.HTTP_API,
        backend=ActionBackend.CONFIG_FRAMEWORK,
        purpose=ActionPurpose.TEST,
        target="recorder-failure-isolation",
    )
    dispatcher = ActionDispatcher()
    dispatcher.register(
        action_id="test.recorder.failure.isolation",
        route=route,
        handler=action,
        mutates=False,
    )
    cleanup = PersistedCleanupCoordinator(
        store=InMemoryCleanupStepStore(),
        steps=(CleanupStepSpec(
            "release",
            cleanup_action,
            cleanup_verify,
            release_authority=True,
        ),),
    )
    case = TestCaseSpec(
        case_id="Recorder-Failure-Isolation-001",
        version=1,
        name="Recorder failure isolation",
        suite_id="automation-runtime",
        entry=ActionEntry.WEB,
        contract_status=TestContractStatus.ACTIVE,
        environment_profile="unit",
        parameters={},
        snapshot=(),
        steps=(ActionStepSpec(
            action="test.recorder.failure.isolation",
            purpose=ActionPurpose.TEST,
            args={},
        ),),
        assertions=(AssertionSpec(
            assertion_id="entry_ok",
            source="entry",
            path="ok",
            operator="eq",
            expected=True,
        ),),
        cleanup=CleanupSpec(strategy="restore_snapshot", verify=True),
    )

    result = asyncio.run(AutomationOrchestrator(
        dispatcher=dispatcher,
        events=InMemoryEventBus(),
        assertions=AssertionEngine(),
        cleanup=cleanup,
        recorder=_FailingRuntimeRecorder(),
    ).run(case, run_id="run-recorder-failure", worker_id="unit-worker"))

    assert result.verdict is TestVerdict.PASS
    assert result.state is AutomationRunState.PASSED
    assert cleanup_calls == ["release"]

    operations = {failure.operation for failure in result.recorder_failures}
    assert operations == {"record_state", "record_step", "record_assertions", "finish"}
    assert all(failure.run_id == "run-recorder-failure" for failure in result.recorder_failures)
    assert all(failure.error_type == "RuntimeError" for failure in result.recorder_failures)
    assert all("sensitive" not in repr(failure) for failure in result.recorder_failures)
