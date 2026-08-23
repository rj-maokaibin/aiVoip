from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.capture_v2.bridge import CaptureV2ABBridge
from app.capture_v2.db_models import CaptureEpoch, CaptureLease, CaptureSession
from app.capture_v2.errors import CaptureV2Error
from app.capture_v2.gate.evaluator import GateEvaluator
from app.capture_v2.gate.evidence import GateEvidenceCollector
from app.capture_v2.gate.models import GateCaseResult, GateCheck, GateRunPaths, GateVerdict
from app.capture_v2.producer.manager import ProducerManager
from app.capture_v2.transport.mutator import FencedDeviceMutator
from app.capture_v2.transport.readonly import ReadOnlyDeviceTransport


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bridge(runner, adapter) -> CaptureV2ABBridge:
    return CaptureV2ABBridge(
        session_factory=runner.session_factory,
        adapter=adapter,
        profile_root=runner.profile_root,
        requested_profile_id=runner.requested_profile_id,
    )


def _checkpoint_path(runner, reproduction_session_id: str) -> Path:
    root = Path(runner.output_root) / "reboot_checkpoints"
    root.mkdir(parents=True, exist_ok=True)
    safe = str(reproduction_session_id).replace("/", "-")
    return root / f"{safe}.json"


def _write_checkpoint(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _validate_checkpoint(payload: dict[str, Any], *, reproduction_session_id: str,
                         device_id: str) -> dict[str, Any]:
    if str(payload.get("reproduction_session_id")) != str(reproduction_session_id):
        raise CaptureV2Error("GATE_REBOOT_CHECKPOINT_SESSION_MISMATCH")
    if str(payload.get("device_id")) != str(device_id):
        raise CaptureV2Error("GATE_REBOOT_CHECKPOINT_DEVICE_MISMATCH")
    return payload


def _load_or_rebuild_checkpoint(runner, path: Path, *, reproduction_session_id: str,
                                device_id: str) -> dict[str, Any]:
    """Load an explicit stage-A checkpoint or reconstruct one from durable DB state.

    The reconstruction path salvages historical single-process reboot attempts that
    already persisted CaptureSession/CaptureEpoch/Lease state before the external
    EWEB tunnel disappeared. It is fail-closed: the device lease must still point at
    the exact CaptureSession for this ReproductionSession.
    """
    if path.is_file():
        return _validate_checkpoint(
            json.loads(path.read_text(encoding="utf-8")),
            reproduction_session_id=reproduction_session_id,
            device_id=device_id,
        )

    with runner.session_factory() as db:
        capture = db.scalar(
            select(CaptureSession)
            .where(
                CaptureSession.reproduction_session_id == reproduction_session_id,
                CaptureSession.device_id == device_id,
            )
            .limit(1)
        )
        if capture is None:
            raise CaptureV2Error(
                "GATE_REBOOT_CHECKPOINT_NOT_FOUND",
                details={"reproduction_session_id": reproduction_session_id},
            )
        epoch = db.scalar(
            select(CaptureEpoch)
            .where(CaptureEpoch.capture_session_id == capture.id)
            .order_by(CaptureEpoch.epoch_index.desc())
            .limit(1)
        )
        lease = db.get(CaptureLease, device_id)
        if epoch is None or lease is None:
            raise CaptureV2Error("GATE_REBOOT_CHECKPOINT_REBUILD_INCOMPLETE")
        if str(lease.capture_session_id or "") != str(capture.id):
            raise CaptureV2Error(
                "GATE_REBOOT_CHECKPOINT_LEASE_SESSION_MISMATCH",
                details={
                    "lease_capture_session_id": lease.capture_session_id,
                    "capture_session_id": capture.id,
                },
            )
        if not epoch.boot_id or epoch.producer_pid is None or epoch.producer_starttime is None:
            raise CaptureV2Error("GATE_REBOOT_CHECKPOINT_EPOCH_IDENTITY_INCOMPLETE")
        payload = {
            "schema_version": "capture-v2-reboot-checkpoint-v1",
            "state": "REBUILT_FROM_DURABLE_DB",
            "created_at": _utcnow_iso(),
            "reproduction_session_id": reproduction_session_id,
            "device_id": device_id,
            "old_boot_id": epoch.boot_id,
            "before_capture_session_id": capture.id,
            "before_capture_epoch_id": epoch.id,
            "before_capture_epoch_token": epoch.epoch_token,
            "before_lease_epoch": int(lease.lease_epoch),
            "before_lease_expires_at": lease.expires_at.isoformat() if lease.expires_at else None,
            "before_pid": int(epoch.producer_pid),
            "before_starttime": int(epoch.producer_starttime),
            "rebuild_source": "capture_sessions+capture_epochs+capture_leases",
        }
    _write_checkpoint(path, payload)
    return _validate_checkpoint(
        payload,
        reproduction_session_id=reproduction_session_id,
        device_id=device_id,
    )


async def _collect(runner, *, gate_id: str, capture_session_id: str,
                   device_id: str, facts: dict[str, Any]):
    paths = GateRunPaths.create(runner.output_root, gate_id, device_id)
    collector = GateEvidenceCollector(
        session_factory=runner.session_factory,
        adapter=runner.adapter,
        object_root=runner.object_root,
        repo_root=runner.repo_root,
    )
    await collector.collect(
        paths=paths,
        gate_id=gate_id,
        capture_session_id=capture_session_id,
        device_id=device_id,
        facts=facts,
    )
    return GateEvaluator(paths.case_dir).evaluate(gate_id)


async def _wait_checkpoint_lease_expiry(checkpoint: dict[str, Any]) -> None:
    raw = str(checkpoint.get("before_lease_expires_at") or "").strip()
    if not raw:
        return
    expires = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    delay = max(0.0, (expires - datetime.now(timezone.utc)).total_seconds()) + 0.5
    if delay > 0:
        await asyncio.sleep(delay)


async def _r2_reboot_stage_a(runner, *, reproduction_session_id: str,
                             device: Any, worker_id: str, gate_id: str):
    """Persist pre-reboot authority evidence, then request reboot and return.

    This stage intentionally does not wait for the DUT to reconnect. It exists for
    control paths (for example an EWEB tunnel) that are expected to disappear when
    the DUT reboots. Final product validation happens only in R2-05B.
    """
    first = await _bridge(runner, runner.adapter).establish(
        reproduction_session_id=reproduction_session_id,
        device=device,
        worker_id=f"{worker_id}-before-reboot",
    )
    old_boot = await ReadOnlyDeviceTransport(runner.adapter).boot_id()
    checkpoint_path = _checkpoint_path(runner, reproduction_session_id)
    checkpoint = {
        "schema_version": "capture-v2-reboot-checkpoint-v1",
        "state": "REBOOT_REQUESTED",
        "created_at": _utcnow_iso(),
        "reproduction_session_id": reproduction_session_id,
        "device_id": str(device.id),
        "old_boot_id": old_boot,
        "before_capture_session_id": first.capture_session_id,
        "before_capture_epoch_id": first.ownership.capture_epoch_id,
        "before_capture_epoch_token": first.ownership.capture_epoch_token,
        "before_lease_epoch": first.ownership.lease.lease_epoch,
        "before_lease_expires_at": first.ownership.lease.expires_at.isoformat(),
        "before_pid": first.ownership.producer.pid,
        "before_starttime": first.ownership.producer.process_starttime,
    }
    _write_checkpoint(checkpoint_path, checkpoint)

    try:
        await runner.adapter.execute_shell("reboot", timeout=3.0, retries=0)
    except Exception:
        # A successful reboot commonly drops SSH before the command result arrives.
        pass
    try:
        await runner.adapter.disconnect()
    except Exception:
        pass

    facts = {
        "capture_session_id": first.capture_session_id,
        "old_boot_id": old_boot,
        "before_capture_epoch_id": first.ownership.capture_epoch_id,
        "before_lease_epoch": first.ownership.lease.lease_epoch,
        "before_pid": first.ownership.producer.pid,
        "before_starttime": first.ownership.producer.process_starttime,
        "checkpoint_path": str(checkpoint_path),
        "reboot_requested": True,
        "final_gate_pending": "R2-05B",
    }
    result = GateCaseResult(
        gate_id=gate_id,
        verdict=GateVerdict.PASS,
        checks=(
            GateCheck("pre_reboot_checkpoint_persisted", checkpoint_path.is_file(), True, checkpoint_path.is_file()),
            GateCheck("reboot_request_issued", True, True, True),
        ),
        summary="Two-phase reboot checkpoint armed; final R2-05 recovery validation pending stage B.",
        evidence_bundle=str(checkpoint_path),
        facts=facts,
    )
    return result, facts


async def _r2_reboot_stage_b(runner, *, reproduction_session_id: str,
                             device: Any, worker_id: str, gate_id: str):
    """Resume a checkpointed reboot after the external control tunnel is restored."""
    checkpoint_path = _checkpoint_path(runner, reproduction_session_id)
    checkpoint = _load_or_rebuild_checkpoint(
        runner,
        checkpoint_path,
        reproduction_session_id=reproduction_session_id,
        device_id=str(device.id),
    )
    new_boot = await ReadOnlyDeviceTransport(runner.adapter).boot_id()
    old_boot = str(checkpoint.get("old_boot_id") or "")
    if not new_boot or new_boot == old_boot:
        raise CaptureV2Error(
            "GATE_DUT_REBOOT_NOT_OBSERVED",
            details={"old_boot_id": old_boot, "new_boot_id": new_boot},
        )

    await _wait_checkpoint_lease_expiry(checkpoint)
    second = await _bridge(runner, runner.adapter).establish(
        reproduction_session_id=reproduction_session_id,
        device=device,
        worker_id=f"{worker_id}-after-reboot",
    )
    reader = ReadOnlyDeviceTransport(runner.adapter)
    final_owned = await ProducerManager(
        reader,
        FencedDeviceMutator(runner.adapter, reader),
    ).inspect_owned()
    facts = {
        "capture_session_id": second.capture_session_id,
        "old_boot_id": old_boot,
        "new_boot_id": new_boot,
        "before_capture_epoch_id": checkpoint.get("before_capture_epoch_id"),
        "after_capture_epoch_id": second.ownership.capture_epoch_id,
        "before_lease_epoch": checkpoint.get("before_lease_epoch"),
        "after_lease_epoch": second.ownership.lease.lease_epoch,
        "before_pid": checkpoint.get("before_pid"),
        "after_pid": second.ownership.producer.pid,
        "before_starttime": checkpoint.get("before_starttime"),
        "after_starttime": second.ownership.producer.process_starttime,
        "final_owned_count": len(final_owned),
        "recovery_status": second.ownership.recovery.status.value,
        "recovery_classification": second.ownership.recovery.classification.value,
        "checkpoint_path": str(checkpoint_path),
        "checkpoint_source": checkpoint.get("rebuild_source", "explicit_stage_a"),
    }
    result = await _collect(
        runner,
        gate_id=gate_id,
        capture_session_id=second.capture_session_id,
        device_id=device.id,
        facts=facts,
    )
    checkpoint.update({
        "state": "RESUMED",
        "resumed_at": _utcnow_iso(),
        "new_boot_id": new_boot,
        "after_capture_session_id": second.capture_session_id,
        "after_capture_epoch_id": second.ownership.capture_epoch_id,
        "after_lease_epoch": second.ownership.lease.lease_epoch,
        "after_pid": second.ownership.producer.pid,
        "after_starttime": second.ownership.producer.process_starttime,
        "result_verdict": result.verdict.value,
        "evidence_bundle": result.evidence_bundle,
    })
    _write_checkpoint(checkpoint_path, checkpoint)
    return result, facts


async def maybe_run_reboot_resume_ownership_scenario(
    runner, *, reproduction_session_id: str, device: Any, worker_id: str, gate_id: str,
):
    normal = gate_id.upper().replace("_", "-")
    if normal.startswith("R2-05A"):
        return await _r2_reboot_stage_a(
            runner,
            reproduction_session_id=reproduction_session_id,
            device=device,
            worker_id=worker_id,
            gate_id=gate_id,
        )
    if normal.startswith("R2-05B"):
        return await _r2_reboot_stage_b(
            runner,
            reproduction_session_id=reproduction_session_id,
            device=device,
            worker_id=worker_id,
            gate_id=gate_id,
        )
    return None
