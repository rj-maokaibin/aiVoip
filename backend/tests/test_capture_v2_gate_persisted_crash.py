from pathlib import Path

import pytest

from app.capture_v2.gate.faults import (
    FaultInjectingPersister,
    GateFaultPlan,
    GateSimulatedWorkerCrash,
)


class Persister:
    def __init__(self):
        self.calls = 0
        self.store = object()

    def persist(self, segment_id: str, local_path: Path) -> str:
        self.calls += 1
        return f"capture-v2/D/E/{segment_id}.pcap"


def test_persisted_before_ack_crash_happens_only_after_real_persist_returns(tmp_path):
    base = Persister()
    plan = GateFaultPlan(
        persist_fail_count=1,
        metadata={"mode": "PERSISTED_BEFORE_ACK"},
    )
    wrapped = FaultInjectingPersister(base, plan)

    with pytest.raises(GateSimulatedWorkerCrash) as exc:
        wrapped.persist("SEG1", tmp_path / "seg.pcap")

    assert base.calls == 1
    assert exc.value.phase == "PERSISTED_BEFORE_ACK"
    assert exc.value.segment_id == "SEG1"
    assert plan.persist_fail_count == 0

    # One-shot: recovery can call the same persister path without another crash.
    key = wrapped.persist("SEG1", tmp_path / "seg.pcap")
    assert key.endswith("SEG1.pcap")
    assert base.calls == 2
