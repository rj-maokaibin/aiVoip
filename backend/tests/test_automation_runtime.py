from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from app.automation.actions.dispatcher import (
    ActionDispatcher,
    ActionEvidence,
    ActionHandlerResult,
    DispatchContext,
)
from app.automation.assertions.engine import AssertionEngine
from app.automation.assertions.operators import evaluate_operator
from app.automation.assertions.resolver import EvidenceEnvelope, NormalizedEvidenceStore
from app.automation.cleanup import (
    AutomationCleanupError,
    CleanupStepSpec,
    InMemoryCleanupStepStore,
    PersistedCleanupCoordinator,
)
from app.automation.contracts import (
    AssertionSpec,
    DSLValidationError,
    TestVerdict,
    parse_test_case,
)
from app.automation.event_wait import EventWaitTimeout, InMemoryEventBus
from app.automation.orchestrator import (
    AutomationOrchestrator,
    PrecheckResult,
    RuntimeHooks,
)
from app.automation.registry import TestRegistry, TestRegistryError
from app.automation.state_machine import (
    AutomationRunState,
    AutomationStateError,
    AutomationStateMachine,
)
from app.infrastructure.action_route import (
    ActionBackend,
    ActionEntry,
    ActionPurpose,
    ActionRoute,
    ActionTransport,
    RunIntent,
)


def _raw_case() -> dict:
    return {
        "id": "TEST-WEB-001",
        "version": 1,
        "suite_id": "unit-suite",
        "name": "unit_web_case",
        "entry": "web",
        "environment": {"profile": "mock-lab"},
        "parameters": {"extension": "7901", "password_ref": "secret://unit/voip"},
        "snapshot": ["voip.account"],
        "steps": [
            {
                "action": "voip.account.configure",
                "purpose": "test",
                "args": {
                    "extension": "${extension}",
                    "password_ref": "${password_ref}",
                },
            }
        ],
        "assertions": [
            {
                "id": "entry-accepted",
                "source": "entry",
                "path": "result",
                "op": "eq",
                "value": "accepted",
            }
        ],
        "cleanup": {"strategy": "restore_snapshot", "verify": True},
    }


@pytest.mark.parametrize(
    "mutator",
    [
        lambda raw: raw["steps"][0]["args"].update({"command": "cat /etc/passwd"}),
        lambda raw: raw["steps"][0]["args"].update({"url": "http://dut/cgi-bin/luci"}),
        lambda raw: raw["steps"][0]["args"].update({"topic": "device/config"}),
        lambda raw: raw["parameters"].update({"password": "cleartext"}),
        lambda raw: raw["steps"][0].update(
            {"action": "ssh.exec", "purpose": "test", "args": {}}
        ),
    ],
)
def test_dsl_rejects_raw_transport_and_plaintext_secret(mutator):
    raw = _raw_case()
    mutator(raw)
    with pytest.raises(DSLValidationError):
        parse_test_case(raw)


def test_dsl_keeps_secret_references_and_rejects_diagnosis_purpose():
    case = parse_test_case(_raw_case())
    assert case.parameters["password_ref"] == "secret://unit/voip"
    raw = _raw_case()
    raw["steps"][0]["purpose"] = "diagnosis"
    with pytest.raises(DSLValidationError, match="INVALID_ACTION_PURPOSE"):
        parse_test_case(raw)


def test_registry_versions_checksums_and_duplicate_ids(tmp_path: Path):
    raw = _raw_case()
    (tmp_path / "one.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8")
    registry = TestRegistry(tmp_path)
    definition = registry.definition("TEST-WEB-001")
    assert definition.version == 1
    assert len(definition.checksum) == 64

    (tmp_path / "two.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(TestRegistryError, match="DUPLICATE_TEST_ID"):
        TestRegistry(tmp_path)


def test_state_machine_cannot_reach_terminal_before_cleanup_and_report():
    machine = AutomationStateMachine()
    for state in (
        AutomationRunState.PRECHECK,
        AutomationRunState.RESERVE,
        AutomationRunState.SNAPSHOT,
        AutomationRunState.PROVISION,
        AutomationRunState.ARM,
        AutomationRunState.EXECUTE,
        AutomationRunState.ASSERT,
    ):
        machine.transition(state)

    with pytest.raises(AutomationStateError):
        machine.finish(AutomationRunState.FAILED)

    machine.to_cleanup()
    machine.transition(AutomationRunState.VERIFY_CLEANUP)
    machine.transition(AutomationRunState.REPORT)
    machine.finish(AutomationRunState.FAILED)
    assert machine.history[-4:] == [
        AutomationRunState.CLEANUP,
        AutomationRunState.VERIFY_CLEANUP,
        AutomationRunState.REPORT,
        AutomationRunState.FAILED,
    ]


def test_assertion_engine_is_only_deterministic_oracle_and_preserves_refs():
    evidence = NormalizedEvidenceStore()
    timestamp = datetime(2026, 9, 5, tzinfo=timezone.utc)
    route = {
        "entry": "web",
        "transport": "http_api",
        "backend": "config_framework",
        "purpose": "test",
        "target": "voip",
    }
    evidence.put(
        "entry",
        EvidenceEnvelope(
            data={"result": "accepted"},
            evidence_refs=("ev-http-1",),
            source_timestamp=timestamp,
            route=route,
        ),
    )
    spec = AssertionSpec(
        assertion_id="a-entry",
        source="entry",
        path="result",
        operator="eq",
        expected="accepted",
    )
    evaluation = AssertionEngine().evaluate((spec,), evidence)
    assert evaluation.verdict == TestVerdict.PASS
    assert evaluation.results[0].evidence_refs == ("ev-http-1",)
    assert evaluation.results[0].source_timestamp == timestamp.isoformat()
    assert evaluation.results[0].route == route

    failed = AssertionEngine().evaluate(
        (
            AssertionSpec(
                assertion_id="a-fail",
                source="entry",
                path="result",
                operator="eq",
                expected="rejected",
            ),
        ),
        evidence,
    )
    assert failed.verdict == TestVerdict.FAIL

    inconclusive = AssertionEngine().evaluate(
        (
            AssertionSpec(
                assertion_id="a-missing",
                source="sip",
                path="registration.final_status",
                operator="eq",
                expected=200,
            ),
        ),
        evidence,
    )
    assert inconclusive.verdict == TestVerdict.INCONCLUSIVE


@pytest.mark.parametrize(
    ("operator", "actual", "expected"),
    [
        ("eq", "a", "a"),
        ("ne", "a", "b"),
        ("contains", ["a", "b"], "b"),
        ("regex", "SIP/2.0 200 OK", r"200\s+OK"),
        ("gt", 3, 2),
        ("lt", 2, 3),
        ("range", 3, [2, 4]),
        ("count", [1, 2], 2),
        ("duration", {"duration_seconds": 1.5}, [1.0, 2.0]),
    ],
)
def test_v1_assertion_operators(operator, actual, expected):
    assert evaluate_operator(operator, actual, expected) is True


def test_event_wait_consumes_normalized_events():
    async def scenario():
        events = InMemoryEventBus()
        waiter = asyncio.create_task(events.wait_for("sip.registered", timeout=0.5))
        await asyncio.sleep(0)
        await events.emit(
            "sip.registered",
            {"status": 200},
            evidence_refs=("ev-sip-1",),
        )
        event = await waiter
        assert event.payload["status"] == 200
        assert event.evidence_refs == ("ev-sip-1",)
        with pytest.raises(EventWaitTimeout):
            await events.wait_for("sip.never", timeout=0.01)

    asyncio.run(scenario())


def test_dispatcher_web_test_never_uses_ssh_fallback():
    async def scenario():
        calls = {"web": 0, "ssh": 0}

        async def web_handler(context, args):
            calls["web"] += 1
            return ActionHandlerResult(
                success=False,
                evidence=(
                    ActionEvidence(
                        source="entry",
                        data={"result": "rejected"},
                        evidence_refs=("ev-web-reject",),
                    ),
                ),
            )

        async def ssh_handler(context, args):
            calls["ssh"] += 1
            return ActionHandlerResult(
                success=True,
                evidence=(
                    ActionEvidence(
                        source="config_framework",
                        data={"result": "accepted"},
                    ),
                ),
            )

        dispatcher = ActionDispatcher()
        dispatcher.register(
            action_id="voip.account.configure",
            route=ActionRoute(
                entry=ActionEntry.WEB,
                transport=ActionTransport.HTTP_API,
                backend=ActionBackend.CONFIG_FRAMEWORK,
                purpose=ActionPurpose.TEST,
                target="voip",
            ),
            handler=web_handler,
            mutates=True,
        )
        dispatcher.register(
            action_id="voip.account.configure",
            route=ActionRoute(
                entry=ActionEntry.NONE,
                transport=ActionTransport.SSH,
                backend=ActionBackend.CONFIG_FRAMEWORK,
                purpose=ActionPurpose.TEST,
                target="voip",
            ),
            handler=ssh_handler,
            mutates=True,
        )
        result = await dispatcher.dispatch(
            context=DispatchContext(
                run_id="run-no-fallback",
                intent=RunIntent.VERIFY,
                case_entry=ActionEntry.WEB,
            ),
            action_id="voip.account.configure",
            purpose=ActionPurpose.TEST,
            args={"extension": "7901"},
        )
        assert result.route.entry == ActionEntry.WEB
        assert result.route.transport == ActionTransport.HTTP_API
        assert calls == {"web": 1, "ssh": 0}

    asyncio.run(scenario())


def test_cleanup_is_crash_resumable_and_releases_authority_last():
    async def scenario():
        store = InMemoryCleanupStepStore()
        calls: list[str] = []
        fail_once = {"restore": True}

        async def stop_action():
            calls.append("stop")
            return {}

        async def stop_verify():
            return True

        async def restore_action():
            calls.append("restore")
            if fail_once["restore"]:
                fail_once["restore"] = False
                raise RuntimeError("simulated-crash")
            return {}

        async def restore_verify():
            return True

        async def release_action():
            calls.append("release")
            return {}

        async def release_verify():
            return True

        steps = (
            CleanupStepSpec("stop", stop_action, stop_verify),
            CleanupStepSpec("restore", restore_action, restore_verify),
            CleanupStepSpec(
                "release_authority",
                release_action,
                release_verify,
                release_authority=True,
            ),
        )
        coordinator = PersistedCleanupCoordinator(store=store, steps=steps)
        with pytest.raises(AutomationCleanupError):
            await coordinator.run(run_id="run-cleanup")

        await coordinator.run(run_id="run-cleanup")
        assert calls == ["stop", "restore", "restore", "release"]
        assert store.verified("run-cleanup") == {
            "stop",
            "restore",
            "release_authority",
        }
        assert calls[-1] == "release"

        with pytest.raises(ValueError, match="AUTHORITY_RELEASE_MUST_BE_LAST"):
            PersistedCleanupCoordinator(
                store=InMemoryCleanupStepStore(),
                steps=(
                    CleanupStepSpec(
                        "release",
                        release_action,
                        release_verify,
                        release_authority=True,
                    ),
                    CleanupStepSpec("restore", restore_action, restore_verify),
                ),
            )

    asyncio.run(scenario())


def _build_cleanup(log: list[str]) -> PersistedCleanupCoordinator:
    async def restore():
        log.append("restore")
        return {}

    async def verify_restore():
        return True

    async def release():
        log.append("release")
        return {}

    async def verify_release():
        return True

    return PersistedCleanupCoordinator(
        store=InMemoryCleanupStepStore(),
        steps=(
            CleanupStepSpec("restore", restore, verify_restore),
            CleanupStepSpec(
                "release_authority",
                release,
                verify_release,
                release_authority=True,
            ),
        ),
    )


def _web_dispatcher(result: str = "accepted", *, unknown: bool = False):
    dispatcher = ActionDispatcher()

    async def handler(context, args):
        return ActionHandlerResult(
            success=result == "accepted",
            unknown_result=unknown,
            evidence=(
                ActionEvidence(
                    source="entry",
                    data={"result": result},
                    evidence_refs=("ev-entry",),
                ),
            ),
        )

    dispatcher.register(
        action_id="voip.account.configure",
        route=ActionRoute(
            entry=ActionEntry.WEB,
            transport=ActionTransport.HTTP_API,
            backend=ActionBackend.CONFIG_FRAMEWORK,
            purpose=ActionPurpose.TEST,
            target="voip",
        ),
        handler=handler,
        mutates=True,
    )
    return dispatcher


def test_orchestrator_terminal_paths_always_cleanup_and_assertion_decides_verdict():
    async def run_case(result: str, *, unknown: bool = False, precheck_ok: bool = True):
        cleanup_log: list[str] = []

        async def precheck(context):
            return PrecheckResult(precheck_ok, None if precheck_ok else "NO_RESOURCE")

        orchestrator = AutomationOrchestrator(
            dispatcher=_web_dispatcher(result, unknown=unknown),
            events=InMemoryEventBus(),
            assertions=AssertionEngine(),
            cleanup=_build_cleanup(cleanup_log),
            hooks=RuntimeHooks(precheck=precheck),
        )
        outcome = await orchestrator.run(
            parse_test_case(_raw_case()),
            run_id=f"run-{result}-{unknown}-{precheck_ok}",
            worker_id="worker-1",
        )
        return outcome, cleanup_log

    passed, pass_cleanup = asyncio.run(run_case("accepted"))
    assert passed.verdict == TestVerdict.PASS
    assert passed.state == AutomationRunState.PASSED
    assert pass_cleanup == ["restore", "release"]

    failed, fail_cleanup = asyncio.run(run_case("rejected"))
    assert failed.verdict == TestVerdict.FAIL
    assert failed.state == AutomationRunState.FAILED
    assert fail_cleanup == ["restore", "release"]

    unknown, unknown_cleanup = asyncio.run(run_case("accepted", unknown=True))
    assert unknown.verdict == TestVerdict.INCONCLUSIVE
    assert unknown.state == AutomationRunState.INCONCLUSIVE
    assert unknown_cleanup == ["restore", "release"]

    blocked, blocked_cleanup = asyncio.run(run_case("accepted", precheck_ok=False))
    assert blocked.verdict == TestVerdict.BLOCKED
    assert blocked.state == AutomationRunState.BLOCKED
    assert blocked_cleanup == ["restore", "release"]

    for outcome in (passed, failed, unknown, blocked):
        assert AutomationRunState.CLEANUP in outcome.state_history
        assert AutomationRunState.VERIFY_CLEANUP in outcome.state_history
        assert AutomationRunState.REPORT in outcome.state_history


def test_lease_conflict_maps_to_blocked_without_skipping_cleanup():
    async def scenario():
        cleanup_log: list[str] = []

        class LeaseBusy(RuntimeError):
            code = "LEASE_BUSY"

        async def reserve(context):
            raise LeaseBusy("busy")

        orchestrator = AutomationOrchestrator(
            dispatcher=_web_dispatcher(),
            events=InMemoryEventBus(),
            assertions=AssertionEngine(),
            cleanup=_build_cleanup(cleanup_log),
            hooks=RuntimeHooks(reserve=reserve),
        )
        outcome = await orchestrator.run(
            parse_test_case(_raw_case()),
            run_id="run-lease-busy",
            worker_id="worker-1",
        )
        assert outcome.verdict == TestVerdict.BLOCKED
        assert outcome.state == AutomationRunState.BLOCKED
        assert cleanup_log[-1] == "release"

    asyncio.run(scenario())


def test_mock_golden_registry_contains_required_contracts():
    repo_root = Path(__file__).resolve().parents[2]
    registry = TestRegistry(repo_root / "profiles" / "tests")
    ids = {definition.case.case_id for definition in registry.all()}
    assert {
        "MOCK-WEB-CONFIG-PASS",
        "MOCK-WEB-CONFIG-FAIL",
        "MOCK-WEB-TIMEOUT-UNKNOWN",
        "MOCK-CLEANUP-RESUME",
        "MOCK-LEASE-CONFLICT",
        "MOCK-NO-SSH-FALLBACK",
    } <= ids
