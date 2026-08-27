from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.capture_v2.db_models import CaptureEpoch, CaptureEvent, CaptureSegment, CaptureSession
from app.capture_v2.enums import CaptureSessionState
from app.capture_v2.errors import CaptureV2Error
from app.capture_v2.finalizer import CaptureV2CaptureFinalizer
from app.capture_v2.transport.mutator import FencedDeviceMutator
from app.capture_v2.transport.shell_scripts import release_fence_script
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base


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
    def __init__(self, values=None, procs=None, epoch_dirs=()):
        self.values = values or {}
        self.procs = procs or []
        self.epoch_dirs = epoch_dirs
        self.boot = "boot-1"

    async def read_text(self, path, missing_ok=False):
        return self.values.get(path)

    async def boot_id(self):
        return self.boot

    async def list_epoch_dirs(self):
        return list(self.epoch_dirs)

    async def list_legacy_ring_dirs(self):
        return []

    async def list_tcpdump_processes(self):
        return list(self.procs)


class FakeAdapter:
    def __init__(self, status=0):
        self.status = status
        self.calls = []

    async def execute_shell(self, command, timeout=None, retries=2):
        self.calls.append((command, retries))
        return Result(stdout="OK", exit_status=self.status)


def _token(session_id="S1", owner="W1", epoch=7):
    return Token(
        device_id="D1",
        capture_session_id=session_id,
        owner_worker_id=owner,
        lease_epoch=epoch,
        expires_at=datetime.now(timezone.utc).replace(year=datetime.now(timezone.utc).year + 1),
    )


def test_release_fence_script_is_fenced_and_removes_control_files():
    script = release_fence_script(lease_epoch=7, operation_id="op1")
    assert "lease_epoch" in script
    assert "AIVOIP_FENCED" in script
    assert "exit 73" in script
    assert 'rm -f "$CONTROL/lease_epoch" "$CONTROL/session_id" "$CONTROL/owner_worker" "$CONTROL/boot_id"' in script
    assert "AIVOIP_FENCE_RELEASED" in script


def test_release_fence_success_uses_single_mutation_no_retry():
    adapter = FakeAdapter()
    mut = FencedDeviceMutator(adapter, FakeReader())
    asyncio.run(mut.release_fence(_token()))
    assert len(adapter.calls) == 1
    command, retries = adapter.calls[0]
    assert retries == 0
    assert 'rm -f "$CONTROL/lease_epoch"' in command
    assert "AIVOIP_FENCE_RELEASED" in command


def test_release_fence_stale_epoch_is_fenced_not_retried():
    adapter = FakeAdapter(status=73)
    mut = FencedDeviceMutator(adapter, FakeReader())
    with pytest.raises(CaptureV2Error) as exc:
        asyncio.run(mut.release_fence(_token()))
    assert exc.value.code == "LEASE_FENCED"
    assert len(adapter.calls) == 1


def test_publish_fence_self_heals_dead_foreign_owner():
    """A stale DUT fence from a dead prior session must be cleared so the new
    reproduction can publish its own epoch (no more permanent LEASE_FENCED)."""
    adapter = FakeAdapter()
    reader = FakeReader(
        values={
            "/tmp/aivoip_capture/control/lease_epoch": "1",
            "/tmp/aivoip_capture/control/session_id": "OLD-SESSION",
            "/tmp/aivoip_capture/control/owner_worker": "reproduction-watch:OLD",
        },
        procs=[],  # no live producer -> dead owner -> safe to take over
    )
    mut = FencedDeviceMutator(adapter, reader)
    asyncio.run(mut.publish_fence(_token(session_id="NEW", owner="NEW-OWNER"), boot_id="B"))
    # First a stale-fence clear mutation, then the actual fence publish.
    assert len(adapter.calls) == 2
    clear_cmd, _ = adapter.calls[0]
    assert "AIVOIP_STALE_FENCE_CLEARED" in clear_cmd
    pub_cmd, _ = adapter.calls[1]
    assert "AIVOIP_FENCE_PUBLISHED" in pub_cmd


def test_publish_fence_preserves_strict_fence_when_foreign_producer_alive():
    """If the stale foreign owner is actually still capturing, the strict fence
    must be preserved (LEASE_FENCED) and the control state must NOT be cleared."""
    adapter = FakeAdapter(status=73)
    reader = FakeReader(
        values={
            "/tmp/aivoip_capture/control/lease_epoch": "1",
            "/tmp/aivoip_capture/control/session_id": "LIVE-SESSION",
            "/tmp/aivoip_capture/control/owner_worker": "reproduction-watch:LIVE",
        },
        procs=[
            SimpleNamespace(
                pid=100,
                starttime=200,
                cmdline="tcpdump -w /tmp/aivoip_capture/epochs/CAP_live_0001_token/x.pcap",
            )
        ],
    )
    mut = FencedDeviceMutator(adapter, reader)
    with pytest.raises(CaptureV2Error) as exc:
        asyncio.run(mut.publish_fence(_token(session_id="NEW", owner="NEW-OWNER"), boot_id="B"))
    assert exc.value.code == "LEASE_FENCED"
    # Only the (blocked) publish ran; no stale-fence clear was issued.
    assert len(adapter.calls) == 1
    assert "AIVOIP_STALE_FENCE_CLEARED" not in adapter.calls[0][0]


# ---------------------------------------------------------------------------
# Finalizer: fence release after evidence is durable
# ---------------------------------------------------------------------------

T0 = datetime(2026, 8, 20, tzinfo=timezone.utc)


def _factory():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    tables = [
        CaptureSession.__table__,
        CaptureEpoch.__table__,
        CaptureEvent.__table__,
        CaptureSegment.__table__,
    ]
    Base.metadata.create_all(engine, tables=tables)
    F = sessionmaker(bind=engine, expire_on_commit=False)
    with F() as db, db.begin():
        db.add(
            CaptureSession(
                id="S", reproduction_session_id="R", device_id="D",
                state=CaptureSessionState.PREPARING.value,
                health_status="HEALTHY", capture_profile_id="p",
                capture_profile_version="1", platform_profile_id="mt7621",
                platform_profile_version="1", effective_profile={},
            )
        )
    return F


class _ProducerManager:
    async def stop_identity(self, token, producer):
        pass

    async def read_exit_stats(self, capture_epoch_token):
        from app.capture_v2.producer.manager import ProducerExitStats

        return ProducerExitStats(10, 10, 0)


class _Pump:
    async def run_final_once(self, **kwargs):
        from app.capture_v2.transfer.pump import PumpResult

        return PumpResult()


class _Lease:
    def validate(self, token):
        return token


class _Mutator:
    def __init__(self):
        self.released = 0

    async def release_fence(self, token, **kwargs):
        self.released += 1


@pytest.mark.asyncio
async def test_finalizer_releases_dut_fence_after_durable_finalize():
    F = _factory()
    with F() as db, db.begin():
        db.add(
            CaptureEpoch(
                id="E", capture_session_id="S", device_id="D", epoch_index=1,
                epoch_token="CAP_E", lease_epoch_started=1, state="RUNNING",
                started_at=T0, producer_pid=10, producer_starttime=20,
            )
        )
        db.add(
            CaptureSegment(
                id="SEG", capture_session_id="S", capture_epoch_id="E", device_id="D",
                segment_seq=1, remote_path="/tmp/seg.pcap", remote_inode=1,
                remote_size=24, state="ACKED", storage_key="k", server_size=24,
                sha256="0" * 64, packet_count=10,
            )
        )
    mutator = _Mutator()
    finalizer = CaptureV2CaptureFinalizer(
        session_factory=F, producer_manager=_ProducerManager(), pump=_Pump(),
        lease_manager=_Lease(), mutator=mutator,
    )
    producer = SimpleNamespace(pid=10, process_starttime=20)
    token = SimpleNamespace(lease_epoch=1)
    result = await finalizer.finalize(
        capture_session_id="S", capture_epoch_id="E", capture_epoch_token="CAP_E",
        producer=producer, token=token,
    )
    assert result.durable is True
    assert result.fence_released is True
    assert mutator.released == 1


@pytest.mark.asyncio
async def test_finalizer_fence_release_failure_keeps_durable_result():
    F = _factory()
    with F() as db, db.begin():
        db.add(
            CaptureEpoch(
                id="E", capture_session_id="S", device_id="D", epoch_index=1,
                epoch_token="CAP_E", lease_epoch_started=1, state="RUNNING",
                started_at=T0, producer_pid=10, producer_starttime=20,
            )
        )
        db.add(
            CaptureSegment(
                id="SEG", capture_session_id="S", capture_epoch_id="E", device_id="D",
                segment_seq=1, remote_path="/tmp/seg.pcap", remote_inode=1,
                remote_size=24, state="ACKED", storage_key="k", server_size=24,
                sha256="0" * 64, packet_count=10,
            )
        )

    class _FailingMutator:
        async def release_fence(self, token, **kwargs):
            raise CaptureV2Error("LEASE_FENCED", details={"epoch": token.lease_epoch})

    finalizer = CaptureV2CaptureFinalizer(
        session_factory=F, producer_manager=_ProducerManager(), pump=_Pump(),
        lease_manager=_Lease(), mutator=_FailingMutator(),
    )
    producer = SimpleNamespace(pid=10, process_starttime=20)
    token = SimpleNamespace(lease_epoch=1)
    result = await finalizer.finalize(
        capture_session_id="S", capture_epoch_id="E", capture_epoch_token="CAP_E",
        producer=producer, token=token,
    )
    assert result.durable is True
    assert result.fence_released is False
