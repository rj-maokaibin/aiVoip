from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.capture_v2.gate.models import GateCaseResult, GateCheck, GateVerdict


def _load(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _checks_verdict(checks: list[GateCheck]) -> GateVerdict:
    if any(c.passed is False for c in checks):
        return GateVerdict.FAIL
    if any(c.passed is None for c in checks):
        return GateVerdict.INCONCLUSIVE
    return GateVerdict.PASS


class GateEvaluator:
    """Deterministic PASS/FAIL evaluator over a Gate evidence bundle."""

    def __init__(self, bundle: Path):
        self.bundle = Path(bundle)
        self.manifest = _load(self.bundle / "manifest.json")

    def _db(self, name: str) -> list[dict[str, Any]]:
        path = self.bundle / "db" / f"{name}.json"
        return _load(path) if path.is_file() else []

    def _store(self) -> list[dict[str, Any]]:
        path = self.bundle / "server" / "store_inventory.json"
        return _load(path) if path.is_file() else []

    def evaluate(self, gate_id: str) -> GateCaseResult:
        normal = gate_id.upper().replace("_", "-")
        if normal.startswith("R1"):
            return self._r1(normal)
        if normal.startswith("R2"):
            return self._r2(normal)
        if normal.startswith("R3"):
            return self._r3(normal)
        return GateCaseResult(
            gate_id=normal,
            verdict=GateVerdict.INCONCLUSIVE,
            checks=(),
            summary="No deterministic evaluator is registered for this Gate ID.",
            evidence_bundle=str(self.bundle),
        )

    def _r1(self, gate_id: str) -> GateCaseResult:
        facts = dict(self.manifest.get("facts") or {})
        checks = [
            GateCheck("exactly_one_winner", facts.get("winner_count") == 1, 1, facts.get("winner_count")),
            GateCheck("exactly_one_loser", facts.get("loser_count") == 1, 1, facts.get("loser_count")),
            GateCheck("loser_is_lease_busy", facts.get("loser_code") == "LEASE_BUSY", "LEASE_BUSY", facts.get("loser_code")),
            GateCheck("single_lease_row", len(self._db("capture_leases")) == 1, 1, len(self._db("capture_leases"))),
        ]
        verdict = _checks_verdict(checks)
        return GateCaseResult(gate_id, verdict, tuple(checks), "PostgreSQL lease race", str(self.bundle), facts)

    def _r2(self, gate_id: str) -> GateCaseResult:
        facts = dict(self.manifest.get("facts") or {})
        epochs = self._db("capture_epochs")
        gaps = self._db("capture_gaps")
        leases = self._db("capture_leases")
        procs = list((self.manifest.get("dut_summary") or {}).get("tcpdump_processes") or [])
        owned = [p for p in procs if "/tmp/aivoip_capture/" in str(p.get("cmdline", ""))]
        checks = [
            GateCheck("exactly_one_owned_producer", len(owned) == 1, 1, len(owned)),
            GateCheck("one_running_epoch", sum(1 for e in epochs if e.get("state") == "RUNNING") == 1, 1,
                      sum(1 for e in epochs if e.get("state") == "RUNNING")),
            GateCheck("lease_active", len(leases) == 1 and leases[0].get("state") == "ACTIVE", "one ACTIVE lease",
                      [{"state": x.get("state"), "lease_epoch": x.get("lease_epoch")} for x in leases]),
            GateCheck("no_unrecovered_gap", not any(g.get("recovered_at") is None for g in gaps), 0,
                      sum(1 for g in gaps if g.get("recovered_at") is None)),
        ]
        if "before_pid" in facts and "after_pid" in facts:
            checks.extend([
                GateCheck("producer_pid_adopted", facts.get("before_pid") == facts.get("after_pid"), facts.get("before_pid"), facts.get("after_pid")),
                GateCheck("producer_starttime_adopted", facts.get("before_starttime") == facts.get("after_starttime"), facts.get("before_starttime"), facts.get("after_starttime")),
                GateCheck("capture_epoch_unchanged", facts.get("before_capture_epoch_id") == facts.get("after_capture_epoch_id"), facts.get("before_capture_epoch_id"), facts.get("after_capture_epoch_id")),
                GateCheck("lease_epoch_increased", int(facts.get("after_lease_epoch", 0)) > int(facts.get("before_lease_epoch", 0)), "> before", facts.get("after_lease_epoch")),
            ])
        verdict = _checks_verdict(checks)
        return GateCaseResult(gate_id, verdict, tuple(checks), "Ownership/recovery invariant", str(self.bundle), facts)

    @staticmethod
    def _error_cause(row: dict[str, Any]) -> str | None:
        detail = row.get("last_error_detail") or {}
        return detail.get("cause") if isinstance(detail, dict) else None

    def _r3(self, gate_id: str) -> GateCaseResult:
        segments = self._db("capture_segments")
        events = self._db("capture_events")
        store = self._store()
        store_by_id = {o.get("segment_id"): o for o in store}
        double_loss = []
        bad_durable = []
        for row in segments:
            obj = store_by_id.get(row.get("id"), {})
            server_exists = obj.get("exists")
            remote_deleted = row.get("state") == "REMOTE_DELETED" or row.get("remote_deleted_at") is not None
            if remote_deleted and server_exists is False:
                double_loss.append(row.get("id"))
            if row.get("state") in ("ACKED", "REMOTE_DELETED") and server_exists is False:
                bad_durable.append(row.get("id"))
        complete_chain = [s for s in segments if s.get("state") == "REMOTE_DELETED"]
        durability_unknown = [
            s.get("id") for s in segments
            if s.get("state") in ("ACKED", "REMOTE_DELETED")
            and store_by_id.get(s.get("id"), {}).get("exists") is None
        ]
        checks = [
            GateCheck("at_least_one_segment", len(segments) > 0, ">0", len(segments)),
            GateCheck("at_least_one_full_chain", len(complete_chain) > 0, ">0 REMOTE_DELETED", len(complete_chain)),
            GateCheck("server_store_observable", None if durability_unknown else True, True, durability_unknown,
                      {"reason": "SERVER_STORE_NOT_OBSERVABLE"} if durability_unknown else {}),
            GateCheck("no_server_and_dut_double_loss", len(double_loss) == 0, [], double_loss),
            GateCheck("acked_segments_have_server_copy", len(bad_durable) == 0, [], bad_durable),
            GateCheck("no_unacked_eviction", not any(s.get("state") == "REMOTE_DELETED" and not s.get("acked_at") for s in segments), True, None),
        ]

        if gate_id.startswith("R3-04"):
            recovered = [s.get("id") for s in segments if s.get("state") == "REMOTE_DELETED" and s.get("last_error_code") == "GATE_INJECTED_AFTER_DURABLE_BEFORE_DB"]
            checks.append(GateCheck("after_durable_before_db_fault_recovered", len(recovered) > 0, ">0 recovered injected segment", recovered))
        elif gate_id.startswith("R3-06"):
            recovered = [s.get("id") for s in segments if s.get("state") == "REMOTE_DELETED" and s.get("last_error_code") == "REMOTE_DELETE_PENDING" and self._error_cause(s) == "MUTATION_RESULT_UNKNOWN"]
            checks.append(GateCheck("ack_response_lost_recovered_idempotently", len(recovered) > 0, ">0 recovered unknown-result segment", recovered))
        elif gate_id.startswith("R3-07"):
            recovered = [s.get("id") for s in segments if s.get("state") == "REMOTE_DELETED" and s.get("last_error_code") == "REMOTE_DELETE_PENDING" and self._error_cause(s) == "DEVICE_MUTATION_FAILED"]
            checks.append(GateCheck("remote_delete_failure_retried", len(recovered) > 0, ">0 recovered delete-failure segment", recovered))
        elif gate_id.startswith("R3-08"):
            repaired_ids = {e.get("entity_id") for e in events if e.get("event_type") == "SEGMENT_SERVER_COPY_REPAIRED"}
            recovered = [s.get("id") for s in segments if s.get("id") in repaired_ids and s.get("state") == "REMOTE_DELETED"]
            checks.append(GateCheck("server_copy_loss_repaired_before_dut_delete", len(recovered) > 0, ">0 repaired then deleted segment", recovered))

        verdict = _checks_verdict(checks)
        return GateCaseResult(gate_id, verdict, tuple(checks), "Reliable Segment / SFTP / ACK", str(self.bundle), dict(self.manifest.get("facts") or {}))
