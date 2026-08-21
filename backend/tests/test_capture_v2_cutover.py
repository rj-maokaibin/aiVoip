import json

from app.capture_v2.cutover.gate import CaptureV2CutoverGate


def test_cutover_blocks_when_real_gates_are_deferred(tmp_path):
    path = tmp_path / "gate.json"
    path.write_text(json.dumps({
        "schema_version": "capture-v2-release-gate-v1",
        "software_gate_passed": True,
        "real_ownership_gate_passed": False,
        "real_segment_gate_passed": False,
        "readiness_gate_passed": False,
        "coverage_gate_passed": False,
        "e2e_gate_passed": False,
        "rollback_gate_passed": False,
        "approved": False,
    }))
    decision = CaptureV2CutoverGate.evaluate(path)
    assert decision.allowed is False
    assert "REAL_OWNERSHIP_GATE_PASSED_FALSE" in decision.reasons


def test_cutover_allows_only_all_true(tmp_path):
    path = tmp_path / "gate.json"
    data = {"schema_version": "capture-v2-release-gate-v1"}
    for key in CaptureV2CutoverGate.REQUIRED_TRUE:
        data[key] = True
    path.write_text(json.dumps(data))
    assert CaptureV2CutoverGate.evaluate(path).allowed is True
