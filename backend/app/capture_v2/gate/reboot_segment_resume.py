from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.capture_v2.c_bridge import CaptureV2CBridge
from app.capture_v2.db_models import CaptureLease, CaptureSegment
from app.capture_v2.errors import CaptureV2Error
from app.capture_v2.gate.evaluator import GateEvaluator
from app.capture_v2.gate.evidence import GateEvidenceCollector
from app.capture_v2.gate.models import GateCaseResult, GateCheck, GateRunPaths, GateVerdict
from app.capture_v2.transport.readonly import ReadOnlyDeviceTransport


async def _wait_current_device_lease_expiry(runner, device_id: str) -> None:
    with runner.session_factory() as db:
        lease = db.get(CaptureLease, device_id)
        expires = lease.expires_at if lease is not None else None
    if expires is None:
        return
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    delay = max(0.0, (expires - datetime.now(timezone.utc)).total_seconds()) + 0.5
    if delay > 0:
        await asyncio.sleep(delay)


def _checkpoint_path(runner, reproduction_session_id: str) -> Path:
    safe = str(reproduction_session_id).replace("/", "-")
    return Path(runner.output_root) / "reboot_checkpoints" / f"{safe}.json"


def _load_reboot_proof(runner, reproduction_session_id: str, device_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    checkpoint_path = _checkpoint_path(runner, reproduction_session_id)
    if not checkpoint_path.is_file():
        raise CaptureV2Error(
            "R3_REBOOT_CHECKPOINT_NOT_FOUND",
            details={"reproduction_session_id": reproduction_session_id},
        )
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    if str(checkpoint.get("reproduction_session_id")) != str(reproduction_session_id):
        raise CaptureV2Error("R3_REBOOT_CHECKPOINT_SESSION_MISMATCH")
    if str(checkpoint.get("device_id")) != str(device_id):
        raise CaptureV2Error("R3_REBOOT_CHECKPOINT_DEVICE_MISMATCH")
    if checkpoint.get("state") != "RESUMED" or checkpoint.get("result_verdict") != "PASS":
        raise CaptureV2Error(
            "R3_REBOOT_CHECKPOINT_NOT_FINAL_PASS",
            details={"state": checkpoint.get("state"), "result_verdict": checkpoint.get("result_verdict")},
        )
    bundle = Path(str(checkpoint.get("evidence_bundle") or ""))
    manifest_path = bundle / "manifest.json"
    if not manifest_path.is_file():
        raise CaptureV2Error("R3_REBOOT_R2_EVIDENCE_MANIFEST_MISSING")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    r2_facts = dict(manifest.get("facts") or {})
    return checkpoint, r2_facts


def _bridge(runner, transport: str) -> CaptureV2CBridge:
    return CaptureV2CBridge(
        session_factory=runner.session_factory,
        adapter=runner.adapter,
        profile_root=runner.profile_root,
        requested_profile_id=runner.requested_profile_id,
        transport=transport,
    )


def _final_verdict(checks: tuple[GateCheck, ...]) -> GateVerdict:
    if any(check.passed is False for check in checks):
        return GateVerdict.FAIL
    if any(check.passed is None for check in checks):
        return GateVerdict.INCONCLUSIVE
    return GateVerdict.PASS


async def _r3_reboot_split_resume(runner, *, reproduction_session_id: str,
                                  device: Any, worker_id: str, gate_id: str,
                                  transport: str, duration_seconds: float,
                                  cycle_interval_seconds: float):
    checkpoint, r2_facts = _load_reboot_proof(
        runner, reproduction_session_id, str(device.id)
    )
    current_boot = await ReadOnlyDeviceTransport(runner.adapter).boot_id()
    old_boot = str(r2_facts.get("old_boot_id") or checkpoint.get("old_boot_id") or "")
    proven_new_boot = str(r2_facts.get("new_boot_id") or checkpoint.get("new_boot_id") or "")

    # A different validation session may have used the device after the reboot.
    # Wait for that authority to expire before re-entering the checkpointed logical
    # reproduction session. This does not fake a reboot; it only serializes access.
    await _wait_current_device_lease_expiry(runner, str(device.id))
    session = await _bridge(runner, transport).establish(
        reproduction_session_id=reproduction_session_id,
        device=device,
        worker_id=f"{worker_id}-post-reboot-continuity",
    )

    from app.capture_v2.gate.missing_r3 import _inject_closed_24b_pcaps
    marker = f"gate_split_reboot_{uuid4().hex[:10]}"
    await _inject_closed_24b_pcaps(session, marker=marker, count=1)
    async with session:
        cycles = await session.drain_for(
            duration_seconds=max(10.0, min(20.0, duration_seconds)),
            cycle_interval_seconds=cycle_interval_seconds,
        )

    with runner.session_factory() as db:
        rows = list(db.query(CaptureSegment).filter(
            CaptureSegment.capture_session_id == session.bootstrap.capture_session_id,
            CaptureSegment.remote_path.contains(marker),
        ).all())
    target = rows[0] if len(rows) == 1 else None
    facts = {
        "capture_session_id": session.bootstrap.capture_session_id,
        "capture_epoch_id": session.bootstrap.ownership.capture_epoch_id,
        "lease_epoch": session.token.lease_epoch,
        "silent_marker": marker,
        "silent_match_count": len(rows),
        "silent_segment_id": getattr(target, "id", None),
        "silent_remote_size": getattr(target, "remote_size", None),
        "silent_server_size": getattr(target, "server_size", None),
        "silent_pcap_valid": getattr(target, "pcap_valid", None),
        "silent_packet_count": getattr(target, "packet_count", None),
        "silent_state": getattr(target, "state", None),
        "silent_persisted_at": bool(getattr(target, "persisted_at", None)),
        "silent_acked_at": bool(getattr(target, "acked_at", None)),
        "silent_remote_deleted_at": bool(getattr(target, "remote_deleted_at", None)),
        "cycles": len(cycles),
        "errors": sum(x.pump.errors for x in cycles),
        "split_control_reboot_proof": True,
        "r2_checkpoint_path": str(_checkpoint_path(runner, reproduction_session_id)),
        "r2_evidence_bundle": checkpoint.get("evidence_bundle"),
        "r2_recovery_classification": r2_facts.get("recovery_classification"),
        "r2_recovery_status": r2_facts.get("recovery_status"),
        "old_boot_id": old_boot,
        "proven_new_boot_id": proven_new_boot,
        "current_boot_id": current_boot,
        "r2_before_capture_epoch_id": r2_facts.get("before_capture_epoch_id"),
        "r2_after_capture_epoch_id": r2_facts.get("after_capture_epoch_id"),
        "r2_before_lease_epoch": r2_facts.get("before_lease_epoch"),
        "r2_after_lease_epoch": r2_facts.get("after_lease_epoch"),
    }

    paths = GateRunPaths.create(runner.output_root, gate_id, str(device.id))
    collector = GateEvidenceCollector(
        session_factory=runner.session_factory,
        adapter=runner.adapter,
        object_root=runner.object_root,
        repo_root=runner.repo_root,
    )
    await collector.collect(
        paths=paths,
        gate_id=gate_id,
        capture_session_id=session.bootstrap.capture_session_id,
        device_id=str(device.id),
        facts=facts,
    )
    # Reuse the deterministic R3-09 chain evaluator for the actual post-reboot
    # segment, then append independent reboot-continuity checks from the audited
    # R2-05 evidence bundle.
    chain = GateEvaluator(paths.case_dir).evaluate("R3-09-SPLIT-REBOOT-CONTINUITY")
    extras = (
        GateCheck(
            "split_reboot_r2_gate_passed",
            checkpoint.get("result_verdict") == "PASS",
            "PASS",
            checkpoint.get("result_verdict"),
        ),
        GateCheck(
            "split_reboot_classified_dut_reboot",
            r2_facts.get("recovery_classification") == "DUT_REBOOT",
            "DUT_REBOOT",
            r2_facts.get("recovery_classification"),
        ),
        GateCheck(
            "split_reboot_boot_id_changed_and_still_current",
            bool(old_boot) and bool(proven_new_boot) and old_boot != proven_new_boot and current_boot == proven_new_boot,
            {"old_not_equal_new": True, "current_equals_proven_new": True},
            {"old": old_boot, "proven_new": proven_new_boot, "current": current_boot},
        ),
        GateCheck(
            "split_reboot_new_capture_epoch_proven",
            bool(r2_facts.get("before_capture_epoch_id"))
            and r2_facts.get("before_capture_epoch_id") != r2_facts.get("after_capture_epoch_id"),
            "new CaptureEpoch",
            {"before": r2_facts.get("before_capture_epoch_id"), "after": r2_facts.get("after_capture_epoch_id")},
        ),
        GateCheck(
            "split_reboot_lease_epoch_increased",
            int(r2_facts.get("after_lease_epoch") or 0) > int(r2_facts.get("before_lease_epoch") or 0),
            "> before",
            r2_facts.get("after_lease_epoch"),
        ),
        GateCheck(
            "split_reboot_post_boot_segment_remote_deleted",
            target is not None and getattr(target, "state", None) == "REMOTE_DELETED",
            "REMOTE_DELETED",
            getattr(target, "state", None),
        ),
    )
    checks = tuple(chain.checks) + extras
    return GateCaseResult(
        gate_id=gate_id,
        verdict=_final_verdict(checks),
        checks=checks,
        summary="Split-control DUT reboot + post-reboot reliable Segment continuity",
        evidence_bundle=str(paths.case_dir),
        facts=facts,
    )


async def maybe_run_reboot_segment_resume_scenario(
    runner, *, reproduction_session_id: str, device: Any, worker_id: str,
    gate_id: str, transport: str, duration_seconds: float,
    cycle_interval_seconds: float,
):
    normal = gate_id.upper().replace("_", "-")
    if not normal.startswith("R3-12B"):
        return None
    return await _r3_reboot_split_resume(
        runner,
        reproduction_session_id=reproduction_session_id,
        device=device,
        worker_id=worker_id,
        gate_id=gate_id,
        transport=transport,
        duration_seconds=duration_seconds,
        cycle_interval_seconds=cycle_interval_seconds,
    )
