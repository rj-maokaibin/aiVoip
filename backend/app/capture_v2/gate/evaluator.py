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


def _event_payload(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload") or {}
    return payload if isinstance(payload, dict) else {}


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
        events = self._db("capture_events")
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

        adoption_gate = gate_id.startswith(("R2-01", "R2-03", "R2-04"))
        if adoption_gate and "before_pid" in facts and "after_pid" in facts:
            checks.extend([
                GateCheck("producer_pid_adopted", facts.get("before_pid") == facts.get("after_pid"), facts.get("before_pid"), facts.get("after_pid")),
                GateCheck("producer_starttime_adopted", facts.get("before_starttime") == facts.get("after_starttime"), facts.get("before_starttime"), facts.get("after_starttime")),
                GateCheck("capture_epoch_unchanged", facts.get("before_capture_epoch_id") == facts.get("after_capture_epoch_id"), facts.get("before_capture_epoch_id"), facts.get("after_capture_epoch_id")),
                GateCheck("lease_epoch_increased", int(facts.get("after_lease_epoch", 0)) > int(facts.get("before_lease_epoch", 0)), "> before", facts.get("after_lease_epoch")),
            ])

        if gate_id.startswith("R2-02"):
            orphan_events = [
                e for e in events
                if e.get("event_type") == "RECOVERY_ORPHAN_FOUND"
                and bool(_event_payload(e).get("legacy"))
            ]
            stopped = [
                e for e in events
                if e.get("event_type") == "PRODUCER_STOPPED"
                and int(_event_payload(e).get("pid") or -1) == int(facts.get("legacy_pid") or -2)
            ]
            recovery_gaps = [
                g for g in gaps
                if g.get("reason_code") == "PCAP_PRODUCER_GAP" and g.get("recovered_at")
            ]
            checks.extend([
                GateCheck("legacy_orphan_classified", facts.get("recovery_classification") == "OLD_SESSION_ALIVE", "OLD_SESSION_ALIVE", facts.get("recovery_classification")),
                GateCheck("legacy_orphan_event_recorded", len(orphan_events) > 0, ">0 RECOVERY_ORPHAN_FOUND legacy=true", len(orphan_events)),
                GateCheck("legacy_orphan_stopped_exact_identity", len(stopped) > 0, ">0 exact legacy PRODUCER_STOPPED", len(stopped)),
                GateCheck("legacy_orphan_gap_recorded_and_closed", len(recovery_gaps) > 0, ">0 recovered PCAP_PRODUCER_GAP", len(recovery_gaps)),
                GateCheck("legacy_orphan_new_epoch", facts.get("before_capture_epoch_id") != facts.get("after_capture_epoch_id"), "new CaptureEpoch", {"before": facts.get("before_capture_epoch_id"), "after": facts.get("after_capture_epoch_id")}),
                GateCheck("legacy_orphan_final_single_producer", int(facts.get("final_owned_count", -1)) == 1, 1, facts.get("final_owned_count")),
            ])
        elif gate_id.startswith("R2-03"):
            conflicts = [e for e in events if e.get("event_type") == "RECOVERY_CONFLICT_FOUND"]
            checks.extend([
                GateCheck("multiple_producers_classified", facts.get("recovery_classification") == "MULTIPLE_PRODUCERS", "MULTIPLE_PRODUCERS", facts.get("recovery_classification")),
                GateCheck("multiple_producers_precondition_exactly_two", int(facts.get("initial_owned_count", -1)) == 2, 2, facts.get("initial_owned_count")),
                GateCheck("multiple_producers_never_third", 0 <= int(facts.get("max_owned_count", -1)) <= 2, "<=2", facts.get("max_owned_count")),
                GateCheck("multiple_producers_conflict_event", len(conflicts) > 0, ">0 RECOVERY_CONFLICT_FOUND", len(conflicts)),
                GateCheck("multiple_producers_final_single", int(facts.get("final_owned_count", -1)) == 1, 1, facts.get("final_owned_count")),
            ])
        elif gate_id.startswith("R2-04"):
            checks.extend([
                GateCheck("stale_stop_fenced", facts.get("stale_stop_code") == "LEASE_FENCED", "LEASE_FENCED", facts.get("stale_stop_code")),
                GateCheck("stale_destructive_mutation_fenced", facts.get("stale_delete_code") == "LEASE_FENCED", "LEASE_FENCED", facts.get("stale_delete_code")),
                GateCheck("stale_delete_did_not_mutate", facts.get("stale_delete_sentinel_survived") is True, True, facts.get("stale_delete_sentinel_survived")),
                GateCheck("dead_op_lock_recovered", facts.get("op_lock_recovered") is True, True, facts.get("op_lock_recovered")),
                GateCheck("fencing_final_single_producer", int(facts.get("final_owned_count", -1)) == 1, 1, facts.get("final_owned_count")),
            ])
        elif gate_id.startswith("R2-05"):
            reboot_events = [e for e in events if e.get("event_type") == "DUT_REBOOT_DETECTED"]
            reboot_gaps = [
                g for g in gaps
                if g.get("reason_code") == "DUT_REBOOT_GAP" and g.get("recovered_at")
            ]
            checks.extend([
                GateCheck("dut_boot_id_changed", bool(facts.get("old_boot_id")) and facts.get("old_boot_id") != facts.get("new_boot_id"), "new boot_id", {"old": facts.get("old_boot_id"), "new": facts.get("new_boot_id")}),
                GateCheck("dut_reboot_classified", facts.get("recovery_classification") == "DUT_REBOOT", "DUT_REBOOT", facts.get("recovery_classification")),
                GateCheck("dut_reboot_new_capture_epoch", facts.get("before_capture_epoch_id") != facts.get("after_capture_epoch_id"), "new CaptureEpoch", {"before": facts.get("before_capture_epoch_id"), "after": facts.get("after_capture_epoch_id")}),
                GateCheck("dut_reboot_lease_epoch_increased", int(facts.get("after_lease_epoch", 0)) > int(facts.get("before_lease_epoch", 0)), "> before", facts.get("after_lease_epoch")),
                GateCheck("dut_reboot_gap_recorded_and_closed", len(reboot_gaps) > 0, ">0 recovered DUT_REBOOT_GAP", len(reboot_gaps)),
                GateCheck("dut_reboot_event_recorded", len(reboot_events) > 0, ">0 DUT_REBOOT_DETECTED", len(reboot_events)),
                GateCheck("dut_reboot_final_single_producer", int(facts.get("final_owned_count", -1)) == 1, 1, facts.get("final_owned_count")),
            ])

        verdict = _checks_verdict(checks)
        return GateCaseResult(gate_id, verdict, tuple(checks), "Ownership/recovery invariant", str(self.bundle), facts)

    @staticmethod
    def _error_cause(row: dict[str, Any]) -> str | None:
        detail = row.get("last_error_detail") or {}
        return detail.get("cause") if isinstance(detail, dict) else None

    def _r3(self, gate_id: str) -> GateCaseResult:
        facts = dict(self.manifest.get("facts") or {})
        segments = self._db("capture_segments")
        events = self._db("capture_events")
        gaps = self._db("capture_gaps")
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
        elif gate_id.startswith("R3-05"):
            fault_id = facts.get("fault_segment_id")
            target = next((s for s in segments if s.get("id") == fault_id), None)
            recovered = bool(
                target
                and target.get("state") == "REMOTE_DELETED"
                and target.get("persisted_at")
                and target.get("acked_at")
                and target.get("remote_deleted_at")
            )
            checks.extend([
                GateCheck("persisted_before_ack_fault_segment_recorded", bool(fault_id), ">0 fault segment", fault_id),
                GateCheck("persisted_before_ack_crash_recovered", recovered, "fault segment REMOTE_DELETED after persisted crash", target),
                GateCheck("persisted_before_ack_same_producer", facts.get("before_pid") == facts.get("after_pid") and facts.get("before_starttime") == facts.get("after_starttime"),
                          {"pid": facts.get("before_pid"), "starttime": facts.get("before_starttime")},
                          {"pid": facts.get("after_pid"), "starttime": facts.get("after_starttime")}),
                GateCheck("persisted_before_ack_same_capture_epoch", facts.get("before_capture_epoch_id") == facts.get("after_capture_epoch_id"),
                          facts.get("before_capture_epoch_id"), facts.get("after_capture_epoch_id")),
                GateCheck("persisted_before_ack_lease_epoch_increased", int(facts.get("after_lease_epoch", 0)) > int(facts.get("before_lease_epoch", 0)),
                          "> before", facts.get("after_lease_epoch")),
            ])
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
        elif gate_id.startswith("R3-10"):
            backlog_ids = [str(x) for x in (facts.get("backlog_segment_ids") or [])]
            recovered_ids = {str(x) for x in (facts.get("recovered_backlog_ids") or [])}
            all_recovered = bool(backlog_ids) and all(x in recovered_ids for x in backlog_ids)
            checks.extend([
                GateCheck("spool_backlog_created", int(facts.get("backlog_unacked_count", 0)) > 0, ">0", facts.get("backlog_unacked_count")),
                GateCheck("spool_backlog_has_bytes", int(facts.get("backlog_unacked_bytes", 0)) > 0, ">0 bytes", facts.get("backlog_unacked_bytes")),
                GateCheck("spool_pressure_critical", facts.get("pressure_state") == "CRITICAL" and "UNACKED_BYTES_LIMIT" in (facts.get("pressure_reasons") or []), "CRITICAL/UNACKED_BYTES_LIMIT", {"state": facts.get("pressure_state"), "reasons": facts.get("pressure_reasons")}),
                GateCheck("unacked_segments_remained_on_dut", int(facts.get("backlog_sample_count", 0)) > 0 and int(facts.get("backlog_remote_sample_exists", -1)) == int(facts.get("backlog_sample_count", 0)), "all sampled backlog segments remain on DUT", {"sample": facts.get("backlog_sample_count"), "exists": facts.get("backlog_remote_sample_exists")}),
                GateCheck("spool_backlog_recovered", all_recovered, backlog_ids, sorted(recovered_ids)),
            ])
        elif gate_id.startswith("R3-12"):
            reboot_events = [e for e in events if e.get("event_type") == "DUT_REBOOT_DETECTED"]
            reboot_gaps = [g for g in gaps if g.get("reason_code") == "DUT_REBOOT_GAP" and g.get("recovered_at")]
            post_epoch = facts.get("after_capture_epoch_id")
            post_deleted = [s.get("id") for s in segments if s.get("capture_epoch_id") == post_epoch and s.get("state") == "REMOTE_DELETED"]
            checks.extend([
                GateCheck("segment_reboot_boot_id_changed", bool(facts.get("old_boot_id")) and facts.get("old_boot_id") != facts.get("new_boot_id"), "new boot_id", {"old": facts.get("old_boot_id"), "new": facts.get("new_boot_id")}),
                GateCheck("segment_reboot_classified", facts.get("recovery_classification") == "DUT_REBOOT", "DUT_REBOOT", facts.get("recovery_classification")),
                GateCheck("segment_reboot_new_capture_epoch", facts.get("before_capture_epoch_id") != post_epoch, "new CaptureEpoch", {"before": facts.get("before_capture_epoch_id"), "after": post_epoch}),
                GateCheck("segment_reboot_lease_epoch_increased", int(facts.get("after_lease_epoch", 0)) > int(facts.get("before_lease_epoch", 0)), "> before", facts.get("after_lease_epoch")),
                GateCheck("segment_reboot_gap_recorded_and_closed", len(reboot_gaps) > 0, ">0 recovered DUT_REBOOT_GAP", len(reboot_gaps)),
                GateCheck("segment_reboot_event_recorded", len(reboot_events) > 0, ">0 DUT_REBOOT_DETECTED", len(reboot_events)),
                GateCheck("segment_reboot_post_epoch_transferred", len(post_deleted) > 0, ">0 post-reboot REMOTE_DELETED", post_deleted),
            ])

        verdict = _checks_verdict(checks)
        return GateCaseResult(gate_id, verdict, tuple(checks), "Reliable Segment / SFTP / ACK", str(self.bundle), facts)
