import asyncio
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.capture_v2.errors import CaptureV2Error
from app.capture_v2.gate.faults import FaultInjectingAdapter, FaultInjectingStore, GateFaultInjector, GateFaultPlan


class Adapter:
    def __init__(self):
        self.calls = 0
        self.shell_calls = 0

    async def sftp_get(self, remote_path, local_path, timeout=None):
        self.calls += 1
        Path(local_path).write_bytes(b"pcap")

    async def scp_get(self, remote_path, local_path, timeout=None):
        self.calls += 1
        Path(local_path).write_bytes(b"pcap")

    async def execute_shell(self, command, *args, **kwargs):
        self.shell_calls += 1
        return SimpleNamespace(exit_status=0, stdout="ok", stderr="")


class Store:
    def __init__(self, root: Path):
        self.root = root
        self.calls = 0

    def persist(self, *, source_path, storage_key, sha256):
        self.calls += 1
        target = self.root / storage_key
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(Path(source_path).read_bytes())
        return SimpleNamespace(storage_key=storage_key, size=target.stat().st_size, sha256=sha256)

    def verify(self, *, storage_key, size, sha256):
        target = self.root / storage_key
        return target.is_file() and target.stat().st_size == size and hashlib.sha256(target.read_bytes()).hexdigest() == sha256


def test_gate_fault_plan_injects_sftp_before_and_then_recovers(tmp_path):
    base = Adapter()
    wrapped = FaultInjectingAdapter(base, GateFaultPlan(sftp_fail_before_get_count=1))
    with pytest.raises(CaptureV2Error) as exc:
        asyncio.run(wrapped.sftp_get("/remote", str(tmp_path / "a")))
    assert exc.value.code == "GATE_INJECTED_SFTP_FAILURE"
    asyncio.run(wrapped.sftp_get("/remote", str(tmp_path / "a")))
    assert base.calls == 1


def test_gate_after_durable_before_db_fails_only_after_object_exists(tmp_path):
    src = tmp_path / "src.pcap"
    src.write_bytes(b"durable")
    digest = hashlib.sha256(src.read_bytes()).hexdigest()
    base = Store(tmp_path / "store")
    plan = GateFaultPlan(persist_fail_count=1, metadata={"mode": "AFTER_DURABLE_BEFORE_DB"})
    wrapped = FaultInjectingStore(base, plan)
    with pytest.raises(CaptureV2Error) as exc:
        wrapped.persist(source_path=src, storage_key="capture-v2/D/E/seg_1.pcap", sha256=digest)
    assert exc.value.code == "GATE_INJECTED_AFTER_DURABLE_BEFORE_DB"
    assert (base.root / "capture-v2/D/E/seg_1.pcap").is_file()
    assert base.calls == 1


def test_gate_ack_response_lost_executes_delete_then_drops_response():
    base = Adapter()
    plan = GateFaultPlan(sftp_fail_before_get_count=1, metadata={"mode": "ACK_RESPONSE_LOST_ONCE"})
    wrapped = FaultInjectingAdapter(base, plan)
    command = "rm -f -- /tmp/aivoip_capture/epochs/CAP/seg_0001.pcap"
    with pytest.raises(RuntimeError, match="GATE_INJECTED_ACK_RESPONSE_LOST"):
        asyncio.run(wrapped.execute_shell(command, retries=0))
    assert base.shell_calls == 1
    assert plan.sftp_fail_before_get_count == 0


def test_gate_remote_delete_failure_does_not_execute_delete():
    base = Adapter()
    plan = GateFaultPlan(sftp_fail_before_get_count=1, metadata={"mode": "REMOTE_DELETE_FAIL_ONCE"})
    wrapped = FaultInjectingAdapter(base, plan)
    command = "rm -f -- /tmp/aivoip_capture/epochs/CAP/seg_0001.pcap"
    result = asyncio.run(wrapped.execute_shell(command, retries=0))
    assert result.exit_status == 82
    assert base.shell_calls == 0
    assert plan.sftp_fail_before_get_count == 0


def test_gate_server_copy_loss_is_real_quarantine_before_delete(tmp_path):
    src = tmp_path / "src.pcap"
    src.write_bytes(b"server-copy")
    digest = hashlib.sha256(src.read_bytes()).hexdigest()
    base = Store(tmp_path / "store")
    persisted = base.persist(source_path=src, storage_key="capture-v2/D/E/seg_1.pcap", sha256=digest)
    plan = GateFaultPlan(persist_fail_count=1, metadata={"mode": "SERVER_COPY_LOSS_BEFORE_DELETE"})
    wrapped = FaultInjectingStore(base, plan)
    # FaultInjectingStore defaults to a shared /tmp quarantine for real gate runs.
    # Unit tests must not depend on ownership/permissions left by another runner
    # invocation, so isolate the quarantine while preserving the exact move-before-delete behavior.
    wrapped.quarantine_root = tmp_path / "quarantine"
    wrapped.quarantine_root.mkdir(parents=True, exist_ok=True)
    assert wrapped.verify(storage_key=persisted.storage_key, size=persisted.size, sha256=persisted.sha256) is False
    assert not (base.root / persisted.storage_key).exists()
    assert plan.persist_fail_count == 0


def test_quarantine_is_reversible_and_confined_to_store_root(tmp_path):
    store = tmp_path / "store"
    obj = store / "capture-v2/D/E/seg.pcap"
    obj.parent.mkdir(parents=True)
    obj.write_bytes(b"evidence")
    injector = GateFaultInjector(store_root=store, quarantine_root=tmp_path / "q")
    record = injector.quarantine_server_copy(obj)
    assert not obj.exists()
    assert Path(record["quarantine"]).is_file()
    restored = injector.restore_quarantined(record["token"])
    assert obj.read_bytes() == b"evidence"
    assert restored["original"] == str(obj.resolve())

    outside = tmp_path / "outside.pcap"
    outside.write_bytes(b"x")
    with pytest.raises(CaptureV2Error) as exc:
        injector.quarantine_server_copy(outside)
    assert exc.value.code == "GATE_FAULT_PATH_OUTSIDE_STORE_ROOT"
