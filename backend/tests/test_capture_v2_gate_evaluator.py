import json
from pathlib import Path

from app.capture_v2.gate.evaluator import GateEvaluator
from app.capture_v2.gate.models import GateVerdict


def write(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def base_bundle(tmp_path, *, gate_id, facts=None):
    b = tmp_path / "bundle"
    write(b / "manifest.json", {
        "gate_id": gate_id,
        "facts": facts or {},
        "dut_summary": {"tcpdump_processes": []},
    })
    return b


def test_r1_evaluator_requires_one_winner_one_lease_busy_loser(tmp_path):
    b = base_bundle(tmp_path, gate_id="R1-01", facts={
        "winner_count": 1, "loser_count": 1, "loser_code": "LEASE_BUSY",
    })
    write(b / "db/capture_leases.json", [{"state": "ACTIVE", "lease_epoch": 1}])
    result = GateEvaluator(b).evaluate("R1-01")
    assert result.verdict == GateVerdict.PASS


def test_r2_adopt_evaluator_checks_pid_starttime_epoch_and_higher_fence(tmp_path):
    facts = {
        "before_pid": 100, "after_pid": 100,
        "before_starttime": 200, "after_starttime": 200,
        "before_capture_epoch_id": "E1", "after_capture_epoch_id": "E1",
        "before_lease_epoch": 1, "after_lease_epoch": 2,
    }
    b = base_bundle(tmp_path, gate_id="R2-01", facts=facts)
    manifest = json.loads((b / "manifest.json").read_text())
    manifest["dut_summary"]["tcpdump_processes"] = [
        {"pid": 100, "starttime": 200, "cmdline": "tcpdump -w /tmp/aivoip_capture/epochs/E/active/a.pcap"}
    ]
    write(b / "manifest.json", manifest)
    write(b / "db/capture_epochs.json", [{"id": "E1", "state": "RUNNING"}])
    write(b / "db/capture_gaps.json", [])
    write(b / "db/capture_leases.json", [{"state": "ACTIVE", "lease_epoch": 2}])
    result = GateEvaluator(b).evaluate("R2-01")
    assert result.verdict == GateVerdict.PASS


def test_r3_never_passes_when_server_store_is_unobservable(tmp_path):
    b = base_bundle(tmp_path, gate_id="R3-01")
    write(b / "db/capture_segments.json", [{
        "id": "SEG1", "state": "REMOTE_DELETED", "acked_at": "x", "remote_deleted_at": "x",
    }])
    write(b / "server/store_inventory.json", [{"segment_id": "SEG1", "exists": None}])
    result = GateEvaluator(b).evaluate("R3-01")
    assert result.verdict == GateVerdict.INCONCLUSIVE


def test_r3_fails_double_loss(tmp_path):
    b = base_bundle(tmp_path, gate_id="R3-01")
    write(b / "db/capture_segments.json", [{
        "id": "SEG1", "state": "REMOTE_DELETED", "acked_at": "x", "remote_deleted_at": "x",
    }])
    write(b / "server/store_inventory.json", [{"segment_id": "SEG1", "exists": False}])
    result = GateEvaluator(b).evaluate("R3-01")
    assert result.verdict == GateVerdict.FAIL
