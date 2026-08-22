from __future__ import annotations

import json
from pathlib import Path

from app.capture_v2.gate.evaluator import GateEvaluator
from app.capture_v2.gate.models import GateVerdict


def _write(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _bundle(tmp_path: Path, *, gate_id: str, facts: dict, segments: list[dict]) -> Path:
    root = tmp_path / "bundle"
    _write(root / "manifest.json", {
        "gate_id": gate_id,
        "facts": facts,
        "dut_summary": {"tcpdump_processes": []},
    })
    _write(root / "db/capture_segments.json", segments)
    _write(root / "db/capture_events.json", [])
    _write(root / "db/capture_gaps.json", [])
    _write(root / "server/store_inventory.json", [
        {"segment_id": row["id"], "exists": True} for row in segments
    ])
    return root


def _deleted(segment_id: str, *, size: int = 100, packet_count: int = 1) -> dict:
    return {
        "id": segment_id,
        "state": "REMOTE_DELETED",
        "remote_size": size,
        "server_size": size,
        "pcap_valid": True,
        "packet_count": packet_count,
        "persisted_at": "p",
        "acked_at": "a",
        "remote_deleted_at": "d",
    }


def test_r3_09_requires_real_24b_zero_packet_full_chain(tmp_path):
    segment = _deleted("SILENT", size=24, packet_count=0)
    bundle = _bundle(
        tmp_path,
        gate_id="R3-09",
        facts={"silent_match_count": 1, "silent_segment_id": "SILENT"},
        segments=[segment],
    )
    result = GateEvaluator(bundle).evaluate("R3-09")
    assert result.verdict == GateVerdict.PASS


def test_r3_09_rejects_non_silent_packet_count(tmp_path):
    segment = _deleted("SILENT", size=24, packet_count=1)
    bundle = _bundle(
        tmp_path,
        gate_id="R3-09",
        facts={"silent_match_count": 1, "silent_segment_id": "SILENT"},
        segments=[segment],
    )
    result = GateEvaluator(bundle).evaluate("R3-09")
    assert result.verdict == GateVerdict.FAIL


def test_r3_11_requires_same_producer_epoch_higher_lease_and_exact_backlog(tmp_path):
    segments = [_deleted("A"), _deleted("B")]
    facts = {
        "backlog_unacked_count": 2,
        "backlog_unacked_bytes": 200,
        "backlog_segment_ids": ["A", "B"],
        "backlog_sample_count": 2,
        "backlog_remote_sample_exists": 2,
        "pressure_state": "CRITICAL",
        "pressure_reasons": ["UNACKED_BYTES_LIMIT"],
        "before_pid": 100,
        "after_pid": 100,
        "before_starttime": 200,
        "after_starttime": 200,
        "before_capture_epoch_id": "E1",
        "after_capture_epoch_id": "E1",
        "before_lease_epoch": 7,
        "after_lease_epoch": 8,
        "recovery_classification": "SAME_SESSION_ALIVE",
        "recovery_status": "ADOPTED",
        "recovered_backlog_ids": ["A", "B"],
    }
    bundle = _bundle(tmp_path, gate_id="R3-11", facts=facts, segments=segments)
    result = GateEvaluator(bundle).evaluate("R3-11")
    assert result.verdict == GateVerdict.PASS


def test_r3_11_rejects_new_epoch_replacement(tmp_path):
    segments = [_deleted("A")]
    facts = {
        "backlog_unacked_count": 1,
        "backlog_unacked_bytes": 100,
        "backlog_segment_ids": ["A"],
        "backlog_sample_count": 1,
        "backlog_remote_sample_exists": 1,
        "pressure_state": "CRITICAL",
        "pressure_reasons": ["UNACKED_BYTES_LIMIT"],
        "before_pid": 100,
        "after_pid": 101,
        "before_starttime": 200,
        "after_starttime": 201,
        "before_capture_epoch_id": "E1",
        "after_capture_epoch_id": "E2",
        "before_lease_epoch": 7,
        "after_lease_epoch": 8,
        "recovery_classification": "SAME_SESSION_DEAD",
        "recovery_status": "REPAIRED",
        "recovered_backlog_ids": ["A"],
    }
    bundle = _bundle(tmp_path, gate_id="R3-11", facts=facts, segments=segments)
    result = GateEvaluator(bundle).evaluate("R3-11")
    assert result.verdict == GateVerdict.FAIL
