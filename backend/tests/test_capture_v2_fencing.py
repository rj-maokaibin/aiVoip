import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from app.capture_v2.errors import CaptureV2Error
from app.capture_v2.transport.mutator import FencedDeviceMutator


@dataclass(frozen=True)
class Token:
    device_id: str
    capture_session_id: str
    owner_worker_id: str
    lease_epoch: int
    expires_at: datetime


@dataclass
class Result:
    stdout: str = ""
    stderr: str = ""
    exit_status: int = 0


class FakeReader:
    values = {
        "/tmp/aivoip_capture/control/lease_epoch": "7",
        "/tmp/aivoip_capture/control/session_id": "S1",
        "/tmp/aivoip_capture/control/owner_worker": "W1",
    }

    async def read_text(self, path, missing_ok=False):
        return self.values.get(path)


class FakeAdapter:
    def __init__(self, status=0):
        self.status = status
        self.calls = []

    async def execute_shell(self, command, timeout=None, retries=2):
        self.calls.append((command, retries))
        return Result(stdout="OK", exit_status=self.status)


def _token():
    return Token(
        device_id="D1",
        capture_session_id="S1",
        owner_worker_id="W1",
        lease_epoch=7,
        expires_at=datetime.now(timezone.utc).replace(year=datetime.now(timezone.utc).year + 1),
    )


def test_mutation_forces_zero_transport_retry_and_embeds_fence_check():
    adapter = FakeAdapter()
    mut = FencedDeviceMutator(adapter, FakeReader())
    asyncio.run(mut.execute_fenced(_token(), body="echo DO_IT"))
    command, retries = adapter.calls[0]
    assert retries == 0
    assert 'op.lock' in command
    assert 'lease_epoch' in command
    assert 'AIVOIP_FENCED' in command
    assert 'echo DO_IT' in command


def test_stale_epoch_exit_is_fenced_not_retried():
    adapter = FakeAdapter(status=73)
    mut = FencedDeviceMutator(adapter, FakeReader())
    with pytest.raises(CaptureV2Error) as exc:
        asyncio.run(mut.execute_fenced(_token(), body="echo MUST_NOT_RETRY"))
    assert exc.value.code == "LEASE_FENCED"
    assert len(adapter.calls) == 1


def test_expired_local_token_never_reaches_ssh():
    adapter = FakeAdapter()
    mut = FencedDeviceMutator(adapter, FakeReader())
    token = Token(
        device_id="D1", capture_session_id="S1", owner_worker_id="W1", lease_epoch=7,
        expires_at=datetime(2000, 1, 1, tzinfo=timezone.utc),
    )
    with pytest.raises(CaptureV2Error) as exc:
        asyncio.run(mut.execute_fenced(token, body="echo NEVER"))
    assert exc.value.code == "LEASE_EXPIRED_LOCAL"
    assert adapter.calls == []


def test_expired_local_token_cannot_publish_fence_or_roll_back_epoch():
    adapter = FakeAdapter()
    mut = FencedDeviceMutator(adapter, FakeReader())
    token = Token(
        device_id="D1", capture_session_id="S1", owner_worker_id="W1", lease_epoch=6,
        expires_at=datetime(2000, 1, 1, tzinfo=timezone.utc),
    )
    with pytest.raises(CaptureV2Error) as exc:
        asyncio.run(mut.publish_fence(token, boot_id="BOOT1"))
    assert exc.value.code == "LEASE_EXPIRED_LOCAL"
    assert adapter.calls == []


def test_publish_fence_script_is_monotonic_and_identity_bound():
    adapter = FakeAdapter()
    mut = FencedDeviceMutator(adapter, FakeReader())
    asyncio.run(mut.publish_fence(_token(), boot_id="BOOT1"))
    command, retries = adapter.calls[0]
    assert retries == 0
    assert 'current' in command
    assert '-gt "$requested"' in command
    assert 'current_session' in command
    assert 'current_owner' in command
    assert 'AIVOIP_FENCED' in command


def test_mutations_are_serialized_inside_one_authority_worker():
    class SlowAdapter:
        def __init__(self):
            self.in_flight = 0
            self.max_in_flight = 0

        async def execute_shell(self, command, timeout=None, retries=2):
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            await asyncio.sleep(0.02)
            self.in_flight -= 1
            return Result(stdout="OK", exit_status=0)

    async def scenario():
        adapter = SlowAdapter()
        mut = FencedDeviceMutator(adapter, FakeReader())
        await asyncio.gather(
            mut.execute_fenced(_token(), body="echo ONE"),
            mut.execute_fenced(_token(), body="echo TWO"),
        )
        return adapter.max_in_flight

    assert asyncio.run(scenario()) == 1


def test_op_lock_records_created_at_for_audit_and_stale_recovery():
    adapter = FakeAdapter()
    mut = FencedDeviceMutator(adapter, FakeReader())
    asyncio.run(mut.execute_fenced(_token(), body="echo DO_IT"))
    command, _ = adapter.calls[0]
    assert '"$LOCK/created_at"' in command
    assert 'date +%s' in command
