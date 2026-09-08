from __future__ import annotations

import pytest

from app.automation.cleanup import (
    AutomationCleanupError,
    CleanupStepSpec,
    InMemoryCleanupStepStore,
    PersistedCleanupCoordinator,
)


@pytest.mark.asyncio
async def test_restore_failure_does_not_prevent_authority_release_last() -> None:
    store = InMemoryCleanupStepStore()
    calls: list[str] = []

    async def restore_action():
        calls.append("restore")
        raise RuntimeError("simulated-restore-failure")

    async def restore_verify():
        raise AssertionError("verify must not run after action failure")

    async def crosscheck_action():
        calls.append("crosscheck")
        return {"mutation": False}

    async def crosscheck_verify():
        calls.append("crosscheck_verify")
        return True

    async def release_action():
        calls.append("release")
        return {"released": True}

    async def release_verify():
        calls.append("release_verify")
        return True

    cleanup = PersistedCleanupCoordinator(
        store=store,
        steps=(
            CleanupStepSpec("restore", restore_action, restore_verify),
            CleanupStepSpec("crosscheck", crosscheck_action, crosscheck_verify),
            CleanupStepSpec(
                "release_device_authority",
                release_action,
                release_verify,
                release_authority=True,
            ),
        ),
    )

    with pytest.raises(AutomationCleanupError, match="CLEANUP_STEP_EXCEPTION:restore:RuntimeError"):
        await cleanup.run(run_id="run-fail-safe-release")

    assert calls == ["restore", "crosscheck", "crosscheck_verify", "release", "release_verify"]
    assert store.verified("run-fail-safe-release") == {
        "crosscheck",
        "release_device_authority",
    }
