import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from app.capture_v2.errors import CaptureV2Error
from app.capture_v2.producer.manager import ProducerManager, ProducerStartSpec
from app.capture_v2.transport.readonly import ProcessRecord


@dataclass(frozen=True)
class Token:
    device_id: str = "D1"
    capture_session_id: str = "S1"
    owner_worker_id: str = "W1"
    lease_epoch: int = 1
    expires_at: datetime = datetime(2026, 8, 20, tzinfo=timezone.utc)


class FakeReader:
    def __init__(self, *, initial=0):
        self.started = initial > 0
        self.duplicate = initial > 1
        self.alive = initial > 0

    async def list_tcpdump_processes(self):
        if not self.alive:
            return []
        base = [
            ProcessRecord(
                pid=101,
                starttime=1001,
                cmdline="/usr/bin/tcpdump -ni br-lan_400 -s 0 -U -G 5 -w /tmp/aivoip_capture/epochs/CAP1/active/capture_%Y%m%d.pcap",
            )
        ]
        if self.duplicate:
            base.append(
                ProcessRecord(
                    pid=102,
                    starttime=1002,
                    cmdline="/usr/bin/tcpdump -ni br-lan_400 -s 0 -U -G 5 -w /tmp/aiVoip_ring_old/capture_%Y%m%d.pcap",
                )
            )
        return base

    async def read_text(self, path, missing_ok=False):
        if path.endswith("/session_id"):
            return "S1"
        return None

    async def process_matches(self, *, pid, starttime):
        return self.alive and pid == 101 and starttime == 1001


class FakeMutator:
    def __init__(self, reader):
        self.reader = reader
        self.calls = 0

    async def execute_fenced(self, token, *, body, operation_id=None):
        self.calls += 1
        if "start-stop-daemon" in body:
            self.reader.alive = True
            self.reader.started = True
        elif "kill \"$PID\"" in body:
            self.reader.alive = False
        return "OK"


def test_start_verifies_exact_identity_and_one_producer():
    reader = FakeReader()
    mutator = FakeMutator(reader)
    manager = ProducerManager(reader, mutator)
    producer = asyncio.run(
        manager.start(Token(), ProducerStartSpec(capture_epoch="CAP1", session_id="S1", interface="br-lan_400"))
    )
    assert producer.pid == 101
    assert producer.process_starttime == 1001
    assert producer.capture_epoch == "CAP1"
    assert producer.session_id == "S1"
    assert mutator.calls == 1


def test_start_fails_closed_if_any_owned_producer_exists():
    reader = FakeReader(initial=1)
    mutator = FakeMutator(reader)
    manager = ProducerManager(reader, mutator)
    with pytest.raises(CaptureV2Error) as exc:
        asyncio.run(manager.start(Token(), ProducerStartSpec(capture_epoch="CAP2", session_id="S1", interface="br-lan_400")))
    assert exc.value.code == "CAPTURE_CONFLICT"
    assert mutator.calls == 0


def test_start_fails_if_postcondition_sees_duplicate_owned_producers():
    reader = FakeReader()
    mutator = FakeMutator(reader)
    manager = ProducerManager(reader, mutator)

    original = mutator.execute_fenced
    async def start_and_duplicate(*args, **kwargs):
        result = await original(*args, **kwargs)
        reader.duplicate = True
        return result
    mutator.execute_fenced = start_and_duplicate

    with pytest.raises(CaptureV2Error) as exc:
        asyncio.run(manager.start(Token(), ProducerStartSpec(capture_epoch="CAP1", session_id="S1", interface="br-lan_400")))
    assert exc.value.code == "PRODUCER_DUPLICATED"


def test_stop_uses_exact_pid_starttime_and_verifies_absence():
    reader = FakeReader(initial=1)
    mutator = FakeMutator(reader)
    manager = ProducerManager(reader, mutator)
    producer = asyncio.run(manager.inspect_owned())[0]
    asyncio.run(manager.stop_identity(Token(), producer))
    assert reader.alive is False
    assert mutator.calls == 1


def test_stop_never_uses_fractional_sleep_on_dut_and_escalates_from_controller(monkeypatch):
    reader = FakeReader(initial=1)
    producer = asyncio.run(ProducerManager(reader, FakeMutator(reader)).inspect_owned())[0]

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr("app.capture_v2.producer.manager.asyncio.sleep", no_wait)

    class StickyTermMutator(FakeMutator):
        async def execute_fenced(self, token, *, body, operation_id=None):
            self.calls += 1
            assert "sleep 0.1" not in body
            if "kill -9" in body:
                self.reader.alive = False
            return "OK"

    mutator = StickyTermMutator(reader)
    manager = ProducerManager(reader, mutator)
    asyncio.run(manager.stop_identity(Token(), producer))
    assert reader.alive is False
    assert mutator.calls == 2


def test_start_timeout_but_process_exists_succeeds_via_readback_without_retry():
    reader = FakeReader()

    class UnknownButStarted(FakeMutator):
        async def execute_fenced(self, token, *, body, operation_id=None):
            self.calls += 1
            self.reader.alive = True
            self.reader.started = True
            raise CaptureV2Error("MUTATION_RESULT_UNKNOWN")

    mutator = UnknownButStarted(reader)
    manager = ProducerManager(reader, mutator)
    producer = asyncio.run(
        manager.start(Token(), ProducerStartSpec(capture_epoch="CAP1", session_id="S1", interface="br-lan_400"))
    )
    assert producer.pid == 101
    assert mutator.calls == 1, "unknown START result must be observed, never blindly retried"


def test_start_success_with_no_process_fails_after_postcondition():
    reader = FakeReader()

    class LiesAboutStart(FakeMutator):
        async def execute_fenced(self, token, *, body, operation_id=None):
            self.calls += 1
            return "AIVOIP_PRODUCER_START_REQUESTED"

    mutator = LiesAboutStart(reader)
    manager = ProducerManager(reader, mutator)
    with pytest.raises(CaptureV2Error) as exc:
        asyncio.run(
            manager.start(Token(), ProducerStartSpec(capture_epoch="CAP1", session_id="S1", interface="br-lan_400"))
        )
    assert exc.value.code == "PRODUCER_START_FAILED"
    assert mutator.calls == 1


def test_stop_timeout_but_process_absent_is_success_via_readback():
    reader = FakeReader(initial=1)
    producer = asyncio.run(ProducerManager(reader, FakeMutator(reader)).inspect_owned())[0]

    class UnknownButStopped(FakeMutator):
        async def execute_fenced(self, token, *, body, operation_id=None):
            self.calls += 1
            self.reader.alive = False
            raise CaptureV2Error("MUTATION_RESULT_UNKNOWN")

    mutator = UnknownButStopped(reader)
    manager = ProducerManager(reader, mutator)
    asyncio.run(manager.stop_identity(Token(), producer))
    assert mutator.calls == 1


def test_stop_identity_mismatch_fails_closed_on_pid_reuse():
    reader = FakeReader(initial=1)
    producer = asyncio.run(ProducerManager(reader, FakeMutator(reader)).inspect_owned())[0]

    class IdentityMismatch(FakeMutator):
        async def execute_fenced(self, token, *, body, operation_id=None):
            self.calls += 1
            raise CaptureV2Error("PRODUCER_IDENTITY_MISMATCH")

    mutator = IdentityMismatch(reader)
    manager = ProducerManager(reader, mutator)
    with pytest.raises(CaptureV2Error) as exc:
        asyncio.run(manager.stop_identity(Token(), producer))
    assert exc.value.code == "PRODUCER_IDENTITY_MISMATCH"
    assert reader.alive is True
    assert mutator.calls == 1
