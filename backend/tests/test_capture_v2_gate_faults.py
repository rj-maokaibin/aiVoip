import asyncio
from pathlib import Path

import pytest

from app.capture_v2.errors import CaptureV2Error
from app.capture_v2.gate.faults import FaultInjectingAdapter, GateFaultInjector, GateFaultPlan


class Adapter:
    def __init__(self):
        self.calls = 0

    async def sftp_get(self, remote_path, local_path, timeout=None):
        self.calls += 1
        Path(local_path).write_bytes(b"pcap")


def test_gate_fault_plan_injects_sftp_before_and_then_recovers(tmp_path):
    base = Adapter()
    wrapped = FaultInjectingAdapter(base, GateFaultPlan(sftp_fail_before_get_count=1))
    with pytest.raises(CaptureV2Error) as exc:
        asyncio.run(wrapped.sftp_get("/remote", str(tmp_path / "a")))
    assert exc.value.code == "GATE_INJECTED_SFTP_FAILURE"
    asyncio.run(wrapped.sftp_get("/remote", str(tmp_path / "a")))
    assert base.calls == 1


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
