from types import SimpleNamespace

from app.capture_v2.control import service_rehearsal_runtime
from app.capture_v2.control.rehearsal_fence_cleanup import stale_fence_cleanup_script
from app.core.config import settings


def test_rehearsal_stale_fence_script_is_narrow_and_fail_closed():
    script = stale_fence_cleanup_script()

    # A live mutation lock or a live Capture V2 tcpdump must stop the reset.
    assert 'AIVOIP_REHEARSAL_OP_LOCK_BUSY' in script
    assert 'exit 75' in script
    assert 'AIVOIP_REHEARSAL_PRODUCER_PRESENT' in script
    assert 'exit 76' in script
    assert '/proc/$oldpid/stat' in script
    assert "awk '{print $22}'" in script

    # The guard shell contains the literal probe text in its own sh -c cmdline;
    # it must exclude only itself while continuing to scan every other process.
    assert 'SELF_PID=$$' in script
    assert '[ "$pid" = "$SELF_PID" ] && continue' in script

    # Only stale control-plane identity is removable. Evidence/epochs and the
    # capture root are intentionally outside the mutation surface.
    assert '"$CONTROL/lease_epoch"' in script
    assert '"$CONTROL/session_id"' in script
    assert '"$CONTROL/owner_worker"' in script
    assert '"$CONTROL/boot_id"' in script
    assert 'rm -rf /tmp/aivoip_capture' not in script
    assert 'rm -rf "$CONTROL"' not in script
    assert 'rm -rf /tmp/aivoip_capture/epochs' not in script


def test_enqueue_start_clears_rehearsal_fence_before_v2_queue(monkeypatch):
    calls = []

    async def fake_clear(session_id):
        calls.append(("clear", session_id))
        return {"cleared": True, "scope": "ACTIVATION_REHEARSAL_ONLY"}

    class FakeTask:
        @staticmethod
        def apply_async(*, args, queue):
            calls.append(("queue", args[0], queue))
            return SimpleNamespace(id="TASK-1")

    monkeypatch.setattr(settings, "capture_engine_version", "V2")
    monkeypatch.setattr(service_rehearsal_runtime, "_clear_rehearsal_fence", fake_clear)

    import app.workers.reproduction_tasks as tasks
    monkeypatch.setattr(tasks, "start_reproduction", FakeTask())

    result = service_rehearsal_runtime.enqueue_start(SimpleNamespace(session_id="S1"))

    assert calls == [
        ("clear", "S1"),
        ("queue", "S1", "reproduction-control"),
    ]
    assert result["queued"] is True
    assert result["stale_fence_cleanup"] == {
        "cleared": True,
        "scope": "ACTIVATION_REHEARSAL_ONLY",
    }


def test_enqueue_start_skips_v2_fence_guard_for_v1_rollback_health(monkeypatch):
    calls = []

    async def forbidden_clear(session_id):
        calls.append(("unexpected-clear", session_id))
        raise AssertionError("V1 rollback health must not call V2 rehearsal fence cleanup")

    class FakeTask:
        @staticmethod
        def apply_async(*, args, queue):
            calls.append(("queue", args[0], queue))
            return SimpleNamespace(id="TASK-V1")

    monkeypatch.setattr(settings, "capture_engine_version", "V1")
    monkeypatch.setattr(service_rehearsal_runtime, "_clear_rehearsal_fence", forbidden_clear)

    import app.workers.reproduction_tasks as tasks
    monkeypatch.setattr(tasks, "start_reproduction", FakeTask())

    result = service_rehearsal_runtime.enqueue_start(SimpleNamespace(session_id="V1-S1"))

    assert calls == [("queue", "V1-S1", "reproduction-control")]
    assert result["queued"] is True
    assert result["stale_fence_cleanup"] == {
        "cleared": False,
        "scope": "V1_ROLLBACK_HEALTH_SKIP",
        "reason": "CAPTURE_V2_NOT_SELECTED",
    }
