from __future__ import annotations

import asyncio
import json
import stat

import pytest

from app.automation.gates.g0_recovery import G0RecoveryMarkerStore
from app.automation.gates.golden_cfg_config import GoldenCfgConfigGate, original_display_name
from app.infrastructure.config_framework.schema import ConfigResult
from tools.observe_g0_recovery_state import derive_recovery_candidate, parse_historical_disnames


class _ReadbackConfig:
    def __init__(self, result: ConfigResult) -> None:
        self.result = result
        self.calls = 0

    async def get(self, module: str, *, timeout: float | None = None) -> ConfigResult:
        assert module == "voipUserInfo"
        assert timeout == 3.0
        self.calls += 1
        return self.result


def _gate_for_restore_verify(*, snapshot, readback, store, run_id="run-001"):
    gate = object.__new__(GoldenCfgConfigGate)
    gate.runtime = {"snapshot": snapshot}
    gate.config = _ReadbackConfig(readback)
    gate.command_timeout = 3.0
    gate.recovery_store = store
    gate.run_id = run_id
    return gate


def _readback(disname: str) -> ConfigResult:
    payload = {"data": [{"disName": disname}]}
    return ConfigResult(
        rcode="00000000",
        rmsg="success",
        data=payload["data"],
        raw=payload,
    )


def test_recovery_marker_persists_exact_minimum_scope_and_permissions(tmp_path):
    root = tmp_path / "g0-recovery"
    store = G0RecoveryMarkerStore(root)

    store.write(
        run_id="run-001",
        device_id="dut-001",
        original_disname="7900",
    )

    marker = root / "run-001.json"
    payload = json.loads(marker.read_text(encoding="utf-8"))

    assert set(payload) == {
        "schema",
        "run_id",
        "device_id",
        "module",
        "field",
        "original_disname",
    }
    assert payload == {
        "schema": "g0-recovery-marker-v1",
        "run_id": "run-001",
        "device_id": "dut-001",
        "module": "voipUserInfo",
        "field": "disName",
        "original_disname": "7900",
    }
    lowered = json.dumps(payload, ensure_ascii=False).lower()
    for forbidden in ("passwd", "password", "authid", "secret", "token"):
        assert forbidden not in lowered

    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600
    assert store.read_for_recovery(run_id="run-001", device_id="dut-001") == "7900"


def test_recovery_marker_rejects_unsafe_run_id_and_identity_mismatch(tmp_path):
    store = G0RecoveryMarkerStore(tmp_path / "g0-recovery")
    with pytest.raises(ValueError, match="G0_RECOVERY_RUN_ID_INVALID"):
        store.write(run_id="../escape", device_id="dut-001", original_disname="old")

    store.write(run_id="run-001", device_id="dut-001", original_disname="old")
    with pytest.raises(RuntimeError, match="G0_RECOVERY_IDENTITY_MISMATCH"):
        store.read_for_recovery(run_id="run-001", device_id="other-dut")


def test_original_display_name_extracts_only_mutated_field():
    snapshot = {
        "data": [{
            "number": "7900",
            "authId": "auth-should-not-be-persisted",
            "passwd": "secret-should-not-be-persisted",
            "disName": "original-name",
        }]
    }
    assert original_display_name(snapshot) == "original-name"


def test_restore_reverse_verify_removes_marker_only_after_match(tmp_path):
    store = G0RecoveryMarkerStore(tmp_path / "g0-recovery")
    store.write(run_id="run-001", device_id="dut-001", original_disname="old")
    snapshot = {"data": [{"disName": "old"}]}
    gate = _gate_for_restore_verify(
        snapshot=snapshot,
        readback=_readback("old"),
        store=store,
    )

    restored, details = asyncio.run(gate._restore_verify())

    assert restored is True
    assert details["restore_readback_success"] is True
    assert details["recovery_marker_retained"] is False
    assert store.retained(run_id="run-001") is False


def test_restore_reverse_verify_retains_marker_when_state_does_not_match(tmp_path):
    store = G0RecoveryMarkerStore(tmp_path / "g0-recovery")
    store.write(run_id="run-001", device_id="dut-001", original_disname="old")
    snapshot = {"data": [{"disName": "old"}]}
    gate = _gate_for_restore_verify(
        snapshot=snapshot,
        readback=_readback("G0"),
        store=store,
    )

    restored, details = asyncio.run(gate._restore_verify())

    assert restored is False
    assert details["restore_readback_success"] is True
    assert details["recovery_marker_retained"] is True
    assert store.retained(run_id="run-001") is True
    assert store.read_for_recovery(run_id="run-001", device_id="dut-001") == "old"


def test_history_parser_persists_only_disname_fragment_and_source_position():
    raw = (
        '/tmp/voip_ipc_cli_log.txt:10:"disName":"7900"\n'
        '/tmp/voip_ipc_cli_log.txt:11:"disName" : "G0"\n'
        '/tmp/voip_log.txt:21:"disName":"7900"\n'
        '/tmp/voip_log.txt:22:"disName":"G0"\n'
        '/tmp/other.log:23:"disName":"should-ignore"\n'
        '/tmp/voip_log.txt:24:"passwd":"must-not-parse"\n'
    )
    rows = parse_historical_disnames(raw)
    assert rows == [
        {"source": "/tmp/voip_ipc_cli_log.txt", "line": 10, "disName": "7900"},
        {"source": "/tmp/voip_ipc_cli_log.txt", "line": 11, "disName": "G0"},
        {"source": "/tmp/voip_log.txt", "line": 21, "disName": "7900"},
        {"source": "/tmp/voip_log.txt", "line": 22, "disName": "G0"},
    ]
    serialized = json.dumps(rows, ensure_ascii=False).lower()
    assert "passwd" not in serialized
    assert "authid" not in serialized


def test_recovery_candidate_requires_prior_value_before_failed_marker():
    rows = [
        {"source": "/tmp/voip_ipc_cli_log.txt", "line": 10, "disName": "7900"},
        {"source": "/tmp/voip_ipc_cli_log.txt", "line": 11, "disName": "G0"},
        {"source": "/tmp/voip_log.txt", "line": 20, "disName": "7900"},
        {"source": "/tmp/voip_log.txt", "line": 21, "disName": "G0"},
    ]
    candidate = derive_recovery_candidate(rows, failed_probe_marker="G0")
    assert candidate["consensus"] == "7900"
    assert candidate["confidence"] == "HIGH"
    assert candidate["sources_with_prior"] == 2


def test_recovery_candidate_refuses_conflicting_or_marker_only_history():
    conflicting = [
        {"source": "/tmp/voip_ipc_cli_log.txt", "line": 10, "disName": "7900"},
        {"source": "/tmp/voip_ipc_cli_log.txt", "line": 11, "disName": "G0"},
        {"source": "/tmp/voip_log.txt", "line": 20, "disName": "7901"},
        {"source": "/tmp/voip_log.txt", "line": 21, "disName": "G0"},
    ]
    candidate = derive_recovery_candidate(conflicting, failed_probe_marker="G0")
    assert candidate["consensus"] is None
    assert candidate["confidence"] == "NONE"

    marker_only = [
        {"source": "/tmp/voip_log.txt", "line": 21, "disName": "G0"},
    ]
    candidate = derive_recovery_candidate(marker_only, failed_probe_marker="G0")
    assert candidate["consensus"] is None
    assert candidate["confidence"] == "NONE"
