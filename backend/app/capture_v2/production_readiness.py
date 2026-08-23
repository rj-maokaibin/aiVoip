from __future__ import annotations

import asyncio
import os
import shlex
import tempfile
from pathlib import Path
from typing import Any

from app.capture_v2.d_bridge import CaptureV2DSession
from app.capture_v2.enums import ReadinessStatus
from app.capture_v2.readiness.stage1 import CapturePathChecks
from app.capture_v2.readiness.watchdog import CaptureWatchdog, WatchdogInputs


def _probe_local_store(store: Any) -> tuple[bool, str | None]:
    """Prove the configured server-side durable store is writable and fsync-capable."""
    root = Path(getattr(store, "root", ""))
    if not str(root):
        return False, "STORE_ROOT_UNAVAILABLE"
    path: Path | None = None
    try:
        root.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".capture-v2-production-readiness.", dir=str(root))
        path = Path(name)
        with os.fdopen(fd, "wb") as fh:
            fh.write(b"capture-v2-production-readiness\n")
            fh.flush()
            os.fsync(fh.fileno())
        dfd = os.open(str(root), os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
        return path.is_file() and path.stat().st_size > 0, None
    except Exception as exc:
        return False, f"{type(exc).__name__}:{exc}"
    finally:
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass


async def _probe_transfer(downloader: Any) -> tuple[bool, int, str | None]:
    """Prove the selected Capture V2 exact-download transport before OFFHOOK."""
    fd, name = tempfile.mkstemp(prefix="capture-v2-production-transfer-")
    os.close(fd)
    local = Path(name)
    local.unlink(missing_ok=True)
    try:
        await downloader.get(
            remote_path="/etc/openwrt_release",
            local_path=local,
            timeout=30,
        )
        size = int(local.stat().st_size) if local.is_file() else 0
        return size > 0, size, None
    except Exception as exc:
        return False, 0, f"{type(exc).__name__}:{exc}"
    finally:
        local.unlink(missing_ok=True)


def _pressure_limits(effective_profile: Any) -> tuple[int | None, float | None]:
    resolved = dict(getattr(effective_profile, "resolved", {}) or {})
    resource = dict(resolved.get("platform_resource") or {})
    spool = dict(resolved.get("spool") or {})
    max_bytes = resource.get("spool_max_unacked_bytes")
    max_age = spool.get("max_oldest_unacked_seconds")
    return (
        int(max_bytes) if max_bytes is not None else None,
        float(max_age) if max_age is not None else None,
    )


def _arm_control_ready(arm_snapshot: dict[str, Any]) -> tuple[bool, bool]:
    debug = dict(arm_snapshot.get("DEBUG") or {})
    pcm_rx = dict(arm_snapshot.get("PCM_RX") or {})
    pcm_tx = dict(arm_snapshot.get("PCM_TX") or {})
    fxs_ready = bool(
        debug.get("enabled")
        and debug.get("heartbeat")
        and str(debug.get("status") or "") == "HEALTHY"
    )
    pcm_ready = all(
        bool(row.get("configured")) and bool(row.get("enabled"))
        for row in (pcm_rx, pcm_tx)
    )
    return fxs_ready, pcm_ready


async def evaluate_production_stage1(
    *,
    c_session: Any,
    arm_snapshot: dict[str, Any],
    session_factory: Any,
) -> dict[str, Any]:
    """Evaluate and persist the same pre-OFFHOOK Stage-1 contract proven by R4.

    Stage-1 proves that the capture *path* is ready before business activity.  It
    deliberately does not require a business packet or a rotated PCAP file to have
    appeared yet.  Data-plane evidence remains a later activity-gated contract.
    """
    bootstrap = c_session.bootstrap
    await asyncio.to_thread(c_session.components["lease"].validate, c_session.token)

    owned = await c_session.components["producer"].inspect_owned()
    expected = bootstrap.ownership.producer
    exact = [
        item for item in owned
        if int(item.pid) == int(expected.pid)
        and int(item.process_starttime) == int(expected.process_starttime)
    ]
    exactly_one = len(owned) == 1 and len(exact) == 1

    voice = bootstrap.voice_context
    voice_ready = bool(voice.gateway_ip and voice.voice_vlan_id and voice.interface)
    active_root = f"/tmp/aivoip_capture/epochs/{bootstrap.ownership.capture_epoch_token}/active"
    active_dir_exists = (
        await c_session.components["reader"].run(
            f"[ -d {shlex.quote(active_root)} ] && echo 1 || echo 0"
        )
    ).strip() == "1"

    fxs_ready, pcm_ready = _arm_control_ready(arm_snapshot)
    store_ready, store_error = _probe_local_store(c_session.components["store"])
    transfer_ready, transfer_bytes, transfer_error = await _probe_transfer(
        c_session.components["downloader"]
    )

    max_bytes, max_age = _pressure_limits(bootstrap.effective_profile)
    pressure = c_session.components["pressure"].evaluate(
        capture_session_id=bootstrap.capture_session_id,
        max_unacked_bytes=max_bytes,
        max_oldest_unacked_seconds=max_age,
    )
    pressure_critical = str(pressure.state).upper() == "CRITICAL"

    lease_active = c_session.control_authority == "ACTIVE"
    watchdog = CaptureWatchdog.evaluate(WatchdogInputs(
        lease_active=lease_active,
        producer_alive=len(exact) == 1,
        producer_count=len(owned),
        fxs_reader_alive=fxs_ready,
        server_store_healthy=store_ready,
        transfer_healthy=transfer_ready,
        spool_critical=pressure_critical,
    ))
    checks = CapturePathChecks(
        lease_active=lease_active,
        exactly_one_producer=exactly_one,
        voice_context_ready=voice_ready,
        pcap_ready=bool(exactly_one and active_dir_exists),
        fxs_ready=fxs_ready,
        pcm_control_ready=pcm_ready,
        server_store_ready=store_ready,
        transfer_ready=transfer_ready,
        storage_guard_ready=not pressure_critical,
        watchdog_ready=watchdog.healthy,
    )
    d_session = CaptureV2DSession(
        capture_session_id=bootstrap.capture_session_id,
        session_factory=session_factory,
        effective_profile=bootstrap.effective_profile.model_dump(),
    )
    decision = d_session.evaluate_stage1(checks)
    ready = decision.status == ReadinessStatus.READY
    return {
        "ready": ready,
        "readiness_status": decision.status.value,
        "readiness_reasons": list(decision.reasons),
        "checks": checks.as_dict(),
        "producer_count": len(owned),
        "exact_producer_count": len(exact),
        "active_dir_exists": active_dir_exists,
        "lease_epoch": int(c_session.token.lease_epoch),
        "store_probe": {"ok": store_ready, "error": store_error},
        "transfer_probe": {
            "ok": transfer_ready,
            "bytes": transfer_bytes,
            "error": transfer_error,
        },
        "spool_pressure": {
            "state": pressure.state,
            "unacked_bytes": int(pressure.unacked_bytes),
            "oldest_unacked_seconds": float(pressure.oldest_unacked_seconds),
            "reasons": list(pressure.reasons),
        },
        "watchdog": {
            "healthy": watchdog.healthy,
            "reasons": list(watchdog.reasons),
        },
    }
