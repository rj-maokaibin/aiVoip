from __future__ import annotations

import asyncio
import shlex
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.collectors.asyncssh_adapter import AsyncSSHDeviceAdapter
from app.capture_v2.bridge import CaptureV2ABBridge
from app.capture_v2.c_bridge import CaptureV2CBridge
from app.capture_v2.db_models import CaptureSegment
from app.capture_v2.errors import CaptureV2Error
from app.capture_v2.gate.evaluator import GateEvaluator
from app.capture_v2.gate.evidence import GateEvidenceCollector
from app.capture_v2.gate.faults import FaultInjectingStore, GateFaultPlan
from app.capture_v2.gate.models import GateRunPaths
from app.capture_v2.lease.manager import CaptureLeaseManager
from app.capture_v2.producer.identity import ProducerIdentity, parse_process_record
from app.capture_v2.producer.manager import ProducerManager
from app.capture_v2.transport.mutator import FencedDeviceMutator
from app.capture_v2.transport.readonly import ReadOnlyDeviceTransport


def _ab_bridge(runner, adapter) -> CaptureV2ABBridge:
    return CaptureV2ABBridge(
        session_factory=runner.session_factory,
        adapter=adapter,
        profile_root=runner.profile_root,
        requested_profile_id=runner.requested_profile_id,
    )


def _c_bridge(runner, adapter, transport: str) -> CaptureV2CBridge:
    return CaptureV2CBridge(
        session_factory=runner.session_factory,
        adapter=adapter,
        profile_root=runner.profile_root,
        requested_profile_id=runner.requested_profile_id,
        transport=transport,
    )


async def _collect(runner, *, adapter, gate_id: str, capture_session_id: str,
                   device_id: str, facts: dict[str, Any]):
    paths = GateRunPaths.create(runner.output_root, gate_id, device_id)
    collector = GateEvidenceCollector(
        session_factory=runner.session_factory,
        adapter=adapter,
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


async def _wait_for_gate_process(reader: ReadOnlyDeviceTransport, marker: str,
                                 *, attempts: int = 40) -> ProducerIdentity:
    for _ in range(attempts):
        for proc in await reader.list_tcpdump_processes():
            if marker not in proc.cmdline:
                continue
            return parse_process_record(proc.pid, proc.starttime, proc.cmdline)
        await asyncio.sleep(0.1)
    raise CaptureV2Error("GATE_PRECONDITION_PRODUCER_START_FAILED", details={"marker": marker})


async def _start_legacy_producer(adapter, *, interface: str) -> ProducerIdentity:
    tag = uuid4().hex[:10]
    root = f"/tmp/aiVoip_ring_gate_{tag}"
    marker = f"{root}/capture_"
    cmd = f'''
ROOT={shlex.quote(root)}
mkdir -p "$ROOT"
/sbin/start-stop-daemon -S -b -m -p "$ROOT/producer.pid" -x /usr/bin/tcpdump -- \
  -ni {shlex.quote(interface)} -s 0 -U -G 5 \
  -w "$ROOT/capture_%Y%m%d_%H%M%S.pcap" \
  >"$ROOT/tcpdump.stdout" 2>"$ROOT/tcpdump.stderr"
'''
    result = await adapter.execute_shell(cmd, retries=0)
    if int(result.exit_status or 0) != 0:
        raise CaptureV2Error("GATE_PRECONDITION_PRODUCER_START_FAILED", details={"kind": "legacy"})
    return await _wait_for_gate_process(ReadOnlyDeviceTransport(adapter), marker)


async def _start_stale_v2_producer(adapter, *, interface: str) -> ProducerIdentity:
    epoch = f"GATE_STALE_{uuid4().hex[:10]}"
    root = f"/tmp/aivoip_capture/epochs/{epoch}"
    marker = f"{root}/active/capture_"
    cmd = f'''
ROOT={shlex.quote(root)}
mkdir -p "$ROOT/active" "$ROOT/spool"
printf '%s' {shlex.quote('gate-stale-session')} > "$ROOT/session_id"
printf '%s' {shlex.quote(interface)} > "$ROOT/interface"
printf '%s' {shlex.quote(epoch)} > "$ROOT/capture_epoch"
/sbin/start-stop-daemon -S -b -m -p "$ROOT/producer.pid" -x /usr/bin/tcpdump -- \
  -ni {shlex.quote(interface)} -s 0 -U -G 5 \
  -w "$ROOT/active/capture_%Y%m%d_%H%M%S.pcap" \
  >"$ROOT/tcpdump.stdout" 2>"$ROOT/tcpdump.stderr"
'''
    result = await adapter.execute_shell(cmd, retries=0)
    if int(result.exit_status or 0) != 0:
        raise CaptureV2Error("GATE_PRECONDITION_PRODUCER_START_FAILED", details={"kind": "stale_v2"})
    identity = await _wait_for_gate_process(ReadOnlyDeviceTransport(adapter), marker)
    return identity.with_session("gate-stale-session")


async def _raw_stop_gate_identity(adapter, producer: ProducerIdentity) -> None:
    pid = int(producer.pid)
    st = int(producer.process_starttime)
    cmd = f'''
PID={pid}
EXPECTED={shlex.quote(str(st))}
if [ -r "/proc/$PID/stat" ]; then
  CUR=$(awk '{{print $22}}' "/proc/$PID/stat" 2>/dev/null || true)
  if [ "$CUR" = "$EXPECTED" ]; then kill -9 "$PID" 2>/dev/null || true; fi
fi
'''
    await adapter.execute_shell(cmd, retries=0)


async def _owned_count(manager: ProducerManager) -> int:
    return len(await manager.inspect_owned())


def _clone_adapter(adapter) -> AsyncSSHDeviceAdapter:
    return AsyncSSHDeviceAdapter(
        ip=adapter.ip,
        port=adapter.port,
        username=adapter.username,
        password=adapter.password,
        aim_prompt=getattr(adapter, "aim_prompt", None),
        aim_executable=getattr(adapter, "aim_executable", "aim"),
        kex_algs=list(getattr(adapter, "kex_algs", []) or []),
    )


async def _reboot_and_reconnect(adapter, *, old_boot_id: str,
                                timeout_seconds: float = 150.0):
    try:
        await adapter.execute_shell("reboot", timeout=3.0, retries=0)
    except Exception:
        # Reboot commonly closes SSH before a command result can be returned.
        pass
    try:
        await adapter.disconnect()
    except Exception:
        pass

    fresh = _clone_adapter(adapter)
    deadline = asyncio.get_running_loop().time() + float(timeout_seconds)
    last_error = None
    while asyncio.get_running_loop().time() < deadline:
        try:
            await fresh.connect()
            boot_id = await ReadOnlyDeviceTransport(fresh).boot_id()
            if boot_id and boot_id != old_boot_id:
                return fresh, boot_id
            await fresh.disconnect()
        except Exception as exc:
            last_error = type(exc).__name__
            try:
                await fresh.disconnect()
            except Exception:
                pass
        await asyncio.sleep(2.0)
    raise CaptureV2Error(
        "GATE_DUT_REBOOT_RECONNECT_TIMEOUT",
        details={"old_boot_id": old_boot_id, "last_error": last_error},
    )


async def _wait_token_expiry(token) -> None:
    expires = token.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    delay = max(0.0, (expires - datetime.now(timezone.utc)).total_seconds()) + 0.5
    await asyncio.sleep(delay)


async def _r2_legacy_orphan(runner, *, reproduction_session_id: str, device: Any,
                            worker_id: str, gate_id: str):
    bridge = _ab_bridge(runner, runner.adapter)
    first = await bridge.establish(
        reproduction_session_id=reproduction_session_id,
        device=device,
        worker_id=f"{worker_id}-setup",
    )
    reader = ReadOnlyDeviceTransport(runner.adapter)
    mutator = FencedDeviceMutator(runner.adapter, reader)
    producer_manager = ProducerManager(reader, mutator)
    lease = CaptureLeaseManager(runner.session_factory, ttl_seconds=30.0)

    await producer_manager.stop_identity(first.ownership.lease, first.ownership.producer)
    lease.release(first.ownership.lease)
    legacy = await _start_legacy_producer(
        runner.adapter,
        interface=first.voice_context.interface,
    )
    try:
        injected_count = await _owned_count(producer_manager)
        second = await bridge.establish(
            reproduction_session_id=reproduction_session_id,
            device=device,
            worker_id=f"{worker_id}-recovery",
        )
        final_owned = await producer_manager.inspect_owned()
        facts = {
            "capture_session_id": second.capture_session_id,
            "legacy_pid": legacy.pid,
            "legacy_starttime": legacy.process_starttime,
            "initial_owned_count": injected_count,
            "final_owned_count": len(final_owned),
            "final_pid": second.ownership.producer.pid,
            "before_capture_epoch_id": first.ownership.capture_epoch_id,
            "after_capture_epoch_id": second.ownership.capture_epoch_id,
            "before_lease_epoch": first.ownership.lease.lease_epoch,
            "after_lease_epoch": second.ownership.lease.lease_epoch,
            "recovery_status": second.ownership.recovery.status.value,
            "recovery_classification": second.ownership.recovery.classification.value,
        }
        result = await _collect(
            runner,
            adapter=runner.adapter,
            gate_id=gate_id,
            capture_session_id=second.capture_session_id,
            device_id=device.id,
            facts=facts,
        )
        return result, facts
    finally:
        # Recovery should have stopped it. If a Gate assertion fails earlier,
        # clean up only the exact PID/starttime created by this harness.
        if await reader.process_matches(pid=legacy.pid, starttime=legacy.process_starttime):
            await _raw_stop_gate_identity(runner.adapter, legacy)


async def _r2_multiple_producers(runner, *, reproduction_session_id: str, device: Any,
                                 worker_id: str, gate_id: str):
    bridge = _ab_bridge(runner, runner.adapter)
    first = await bridge.establish(
        reproduction_session_id=reproduction_session_id,
        device=device,
        worker_id=f"{worker_id}-setup",
    )
    reader = ReadOnlyDeviceTransport(runner.adapter)
    mutator = FencedDeviceMutator(runner.adapter, reader)
    producer_manager = ProducerManager(reader, mutator)
    lease = CaptureLeaseManager(runner.session_factory, ttl_seconds=30.0)
    stale = await _start_stale_v2_producer(
        runner.adapter,
        interface=first.voice_context.interface,
    )
    max_owned = await _owned_count(producer_manager)
    stop_sampling = asyncio.Event()

    async def sampler():
        nonlocal max_owned
        while not stop_sampling.is_set():
            try:
                max_owned = max(max_owned, await _owned_count(producer_manager))
            except Exception:
                pass
            await asyncio.sleep(0.01)

    sample_task = asyncio.create_task(sampler(), name="r2-multiple-producer-sampler")
    try:
        initial_count = await _owned_count(producer_manager)
        lease.release(first.ownership.lease)
        second = await bridge.establish(
            reproduction_session_id=reproduction_session_id,
            device=device,
            worker_id=f"{worker_id}-recovery",
        )
        final_owned = await producer_manager.inspect_owned()
        facts = {
            "capture_session_id": second.capture_session_id,
            "initial_owned_count": initial_count,
            "max_owned_count": max_owned,
            "final_owned_count": len(final_owned),
            "stale_pid": stale.pid,
            "before_pid": first.ownership.producer.pid,
            "after_pid": second.ownership.producer.pid,
            "before_starttime": first.ownership.producer.process_starttime,
            "after_starttime": second.ownership.producer.process_starttime,
            "before_capture_epoch_id": first.ownership.capture_epoch_id,
            "after_capture_epoch_id": second.ownership.capture_epoch_id,
            "before_lease_epoch": first.ownership.lease.lease_epoch,
            "after_lease_epoch": second.ownership.lease.lease_epoch,
            "recovery_status": second.ownership.recovery.status.value,
            "recovery_classification": second.ownership.recovery.classification.value,
        }
        result = await _collect(
            runner,
            adapter=runner.adapter,
            gate_id=gate_id,
            capture_session_id=second.capture_session_id,
            device_id=device.id,
            facts=facts,
        )
        return result, facts
    finally:
        stop_sampling.set()
        try:
            await sample_task
        except Exception:
            pass
        if await reader.process_matches(pid=stale.pid, starttime=stale.process_starttime):
            await _raw_stop_gate_identity(runner.adapter, stale)


async def _r2_stale_fencing(runner, *, reproduction_session_id: str, device: Any,
                            worker_id: str, gate_id: str):
    bridge = _ab_bridge(runner, runner.adapter)
    first = await bridge.establish(
        reproduction_session_id=reproduction_session_id,
        device=device,
        worker_id=f"{worker_id}-a",
    )
    lease = CaptureLeaseManager(runner.session_factory, ttl_seconds=30.0)
    lease.release(first.ownership.lease)
    second = await bridge.establish(
        reproduction_session_id=reproduction_session_id,
        device=device,
        worker_id=f"{worker_id}-b",
    )

    reader = ReadOnlyDeviceTransport(runner.adapter)
    mutator = FencedDeviceMutator(runner.adapter, reader)
    producer_manager = ProducerManager(reader, mutator)

    stale_stop_code = None
    try:
        await producer_manager.stop_identity(first.ownership.lease, second.ownership.producer)
    except CaptureV2Error as exc:
        stale_stop_code = exc.code

    sentinel = "/tmp/aivoip_capture/gate-stale-delete-sentinel"
    await runner.adapter.execute_shell(
        f"printf '%s' gate > {shlex.quote(sentinel)}",
        retries=0,
    )
    stale_delete_code = None
    try:
        await mutator.execute_fenced(
            first.ownership.lease,
            body=f"rm -f -- {shlex.quote(sentinel)}",
        )
    except CaptureV2Error as exc:
        stale_delete_code = exc.code
    sentinel_survived = (await reader.run(
        f"[ -f {shlex.quote(sentinel)} ] && echo 1 || echo 0"
    )).strip() == "1"

    # Manufacture a dead-owner op.lock. The current fenced mutation must safely
    # reclaim it before executing, proving stale lock self-healing.
    await runner.adapter.execute_shell(
        "rm -rf /tmp/aivoip_capture/control/op.lock; "
        "mkdir -p /tmp/aivoip_capture/control/op.lock; "
        "printf '999999' > /tmp/aivoip_capture/control/op.lock/owner_pid; "
        "printf '1' > /tmp/aivoip_capture/control/op.lock/owner_starttime",
        retries=0,
    )
    op_lock_recovered = False
    try:
        await mutator.execute_fenced(
            second.ownership.lease,
            body=f"rm -f -- {shlex.quote(sentinel)}; echo AIVOIP_GATE_LOCK_RECOVERED",
        )
        op_lock_recovered = True
    finally:
        try:
            await runner.adapter.execute_shell(
                f"rm -f -- {shlex.quote(sentinel)}; rm -rf /tmp/aivoip_capture/control/op.lock",
                retries=0,
            )
        except Exception:
            pass

    final_owned = await producer_manager.inspect_owned()
    facts = {
        "capture_session_id": second.capture_session_id,
        "before_lease_epoch": first.ownership.lease.lease_epoch,
        "after_lease_epoch": second.ownership.lease.lease_epoch,
        "before_pid": first.ownership.producer.pid,
        "after_pid": second.ownership.producer.pid,
        "before_starttime": first.ownership.producer.process_starttime,
        "after_starttime": second.ownership.producer.process_starttime,
        "before_capture_epoch_id": first.ownership.capture_epoch_id,
        "after_capture_epoch_id": second.ownership.capture_epoch_id,
        "stale_stop_code": stale_stop_code,
        "stale_delete_code": stale_delete_code,
        "stale_delete_sentinel_survived": sentinel_survived,
        "op_lock_recovered": op_lock_recovered,
        "final_owned_count": len(final_owned),
    }
    result = await _collect(
        runner,
        adapter=runner.adapter,
        gate_id=gate_id,
        capture_session_id=second.capture_session_id,
        device_id=device.id,
        facts=facts,
    )
    return result, facts


async def _r2_dut_reboot(runner, *, reproduction_session_id: str, device: Any,
                         worker_id: str, gate_id: str):
    bridge = _ab_bridge(runner, runner.adapter)
    first = await bridge.establish(
        reproduction_session_id=reproduction_session_id,
        device=device,
        worker_id=f"{worker_id}-before-reboot",
    )
    old_boot = await ReadOnlyDeviceTransport(runner.adapter).boot_id()
    fresh = None
    try:
        fresh, new_boot = await _reboot_and_reconnect(runner.adapter, old_boot_id=old_boot)
        await _wait_token_expiry(first.ownership.lease)
        second = await _ab_bridge(runner, fresh).establish(
            reproduction_session_id=reproduction_session_id,
            device=device,
            worker_id=f"{worker_id}-after-reboot",
        )
        final_owned = await ProducerManager(
            ReadOnlyDeviceTransport(fresh),
            FencedDeviceMutator(fresh, ReadOnlyDeviceTransport(fresh)),
        ).inspect_owned()
        facts = {
            "capture_session_id": second.capture_session_id,
            "old_boot_id": old_boot,
            "new_boot_id": new_boot,
            "before_capture_epoch_id": first.ownership.capture_epoch_id,
            "after_capture_epoch_id": second.ownership.capture_epoch_id,
            "before_lease_epoch": first.ownership.lease.lease_epoch,
            "after_lease_epoch": second.ownership.lease.lease_epoch,
            "before_pid": first.ownership.producer.pid,
            "after_pid": second.ownership.producer.pid,
            "final_owned_count": len(final_owned),
            "recovery_status": second.ownership.recovery.status.value,
            "recovery_classification": second.ownership.recovery.classification.value,
        }
        result = await _collect(
            runner,
            adapter=fresh,
            gate_id=gate_id,
            capture_session_id=second.capture_session_id,
            device_id=device.id,
            facts=facts,
        )
        return result, facts
    finally:
        if fresh is not None:
            try:
                await fresh.disconnect()
            except Exception:
                pass


async def maybe_run_ownership_scenario(runner, *, reproduction_session_id: str,
                                       device: Any, worker_id: str, gate_id: str):
    normal = gate_id.upper().replace("_", "-")
    if normal.startswith("R2-02"):
        return await _r2_legacy_orphan(
            runner, reproduction_session_id=reproduction_session_id,
            device=device, worker_id=worker_id, gate_id=gate_id,
        )
    if normal.startswith("R2-03"):
        return await _r2_multiple_producers(
            runner, reproduction_session_id=reproduction_session_id,
            device=device, worker_id=worker_id, gate_id=gate_id,
        )
    if normal.startswith("R2-04"):
        return await _r2_stale_fencing(
            runner, reproduction_session_id=reproduction_session_id,
            device=device, worker_id=worker_id, gate_id=gate_id,
        )
    if normal.startswith("R2-05"):
        return await _r2_dut_reboot(
            runner, reproduction_session_id=reproduction_session_id,
            device=device, worker_id=worker_id, gate_id=gate_id,
        )
    return None


async def _r3_spool_backlog(runner, *, reproduction_session_id: str, device: Any,
                            worker_id: str, gate_id: str, plan: GateFaultPlan,
                            transport: str, duration_seconds: float,
                            cycle_interval_seconds: float):
    session = await _c_bridge(runner, runner.adapter, transport).establish(
        reproduction_session_id=reproduction_session_id,
        device=device,
        worker_id=worker_id,
    )
    if plan.persist_fail_count <= 0:
        plan.persist_fail_count = 100000
    wrapped = FaultInjectingStore(session.components["store"], plan)
    session.components["store"] = wrapped
    session.components["persister"].store = wrapped
    session.components["reconciler"].store = wrapped
    session.components["reconciler"].persister.store = wrapped

    phase = max(10.0, min(20.0, duration_seconds / 2.0))
    cycles: list[Any] = []
    async with session:
        cycles.extend(await session.drain_for(
            duration_seconds=phase,
            cycle_interval_seconds=cycle_interval_seconds,
        ))
        with runner.session_factory() as db:
            backlog = list(db.query(CaptureSegment).filter(
                CaptureSegment.capture_session_id == session.bootstrap.capture_session_id,
                CaptureSegment.state.notin_(("ACKED", "REMOTE_DELETED")),
            ).all())
            backlog_ids = [row.id for row in backlog]
            backlog_bytes = sum(int(row.remote_size or 0) for row in backlog)
            sample_paths = [row.remote_path for row in backlog[:20]]
        reader = session.components["reader"]
        remote_sample_exists = 0
        for path in sample_paths:
            text = await reader.run(
                f"[ -f {shlex.quote(path)} ] && echo 1 || echo 0"
            )
            if text.strip() == "1":
                remote_sample_exists += 1
        pressure = session.components["pressure"].evaluate(
            capture_session_id=session.bootstrap.capture_session_id,
            max_unacked_bytes=1,
            max_oldest_unacked_seconds=None,
        )

        # Restore the real durable path and drain the exact backlog. The wrapper
        # remains installed but becomes a pass-through once the counter is zero.
        plan.persist_fail_count = 0
        cycles.extend(await session.drain_for(
            duration_seconds=phase,
            cycle_interval_seconds=cycle_interval_seconds,
        ))

    with runner.session_factory() as db:
        final_rows = list(db.query(CaptureSegment).filter(
            CaptureSegment.id.in_(backlog_ids)
        ).all()) if backlog_ids else []
        recovered_backlog_ids = [
            row.id for row in final_rows if row.state in ("ACKED", "REMOTE_DELETED")
        ]
    facts = {
        "capture_session_id": session.bootstrap.capture_session_id,
        "capture_epoch_id": session.bootstrap.ownership.capture_epoch_id,
        "lease_epoch": session.token.lease_epoch,
        "backlog_unacked_count": len(backlog_ids),
        "backlog_unacked_bytes": backlog_bytes,
        "backlog_segment_ids": backlog_ids,
        "backlog_sample_count": len(sample_paths),
        "backlog_remote_sample_exists": remote_sample_exists,
        "pressure_state": pressure.state,
        "pressure_reasons": list(pressure.reasons),
        "recovered_backlog_ids": recovered_backlog_ids,
        "cycles": len(cycles),
        "errors": sum(x.pump.errors for x in cycles),
    }
    return await _collect(
        runner,
        adapter=runner.adapter,
        gate_id=gate_id,
        capture_session_id=session.bootstrap.capture_session_id,
        device_id=device.id,
        facts=facts,
    )


async def _r3_dut_reboot(runner, *, reproduction_session_id: str, device: Any,
                         worker_id: str, gate_id: str, transport: str,
                         duration_seconds: float, cycle_interval_seconds: float):
    first = await _c_bridge(runner, runner.adapter, transport).establish(
        reproduction_session_id=reproduction_session_id,
        device=device,
        worker_id=f"{worker_id}-before-reboot",
    )
    async with first:
        await first.drain_for(
            duration_seconds=max(6.0, min(10.0, duration_seconds / 3.0)),
            cycle_interval_seconds=cycle_interval_seconds,
        )
    old_boot = await ReadOnlyDeviceTransport(runner.adapter).boot_id()
    fresh = None
    try:
        fresh, new_boot = await _reboot_and_reconnect(runner.adapter, old_boot_id=old_boot)
        await _wait_token_expiry(first.token)
        second = await _c_bridge(runner, fresh, transport).establish(
            reproduction_session_id=reproduction_session_id,
            device=device,
            worker_id=f"{worker_id}-after-reboot",
        )
        async with second:
            cycles = await second.drain_for(
                duration_seconds=max(10.0, min(20.0, duration_seconds / 2.0)),
                cycle_interval_seconds=cycle_interval_seconds,
            )
        facts = {
            "capture_session_id": second.bootstrap.capture_session_id,
            "old_boot_id": old_boot,
            "new_boot_id": new_boot,
            "before_capture_epoch_id": first.bootstrap.ownership.capture_epoch_id,
            "after_capture_epoch_id": second.bootstrap.ownership.capture_epoch_id,
            "before_lease_epoch": first.token.lease_epoch,
            "after_lease_epoch": second.token.lease_epoch,
            "before_pid": first.bootstrap.ownership.producer.pid,
            "after_pid": second.bootstrap.ownership.producer.pid,
            "recovery_status": second.bootstrap.ownership.recovery.status.value,
            "recovery_classification": second.bootstrap.ownership.recovery.classification.value,
            "post_reboot_cycles": len(cycles),
            "post_reboot_deleted": sum(x.pump.deleted for x in cycles),
        }
        return await _collect(
            runner,
            adapter=fresh,
            gate_id=gate_id,
            capture_session_id=second.bootstrap.capture_session_id,
            device_id=device.id,
            facts=facts,
        )
    finally:
        if fresh is not None:
            try:
                await fresh.disconnect()
            except Exception:
                pass


async def maybe_run_segment_scenario(runner, *, reproduction_session_id: str,
                                     device: Any, worker_id: str, gate_id: str,
                                     plan: GateFaultPlan, transport: str,
                                     duration_seconds: float,
                                     cycle_interval_seconds: float):
    normal = gate_id.upper().replace("_", "-")
    if normal.startswith("R3-10"):
        return await _r3_spool_backlog(
            runner,
            reproduction_session_id=reproduction_session_id,
            device=device,
            worker_id=worker_id,
            gate_id=gate_id,
            plan=plan,
            transport=transport,
            duration_seconds=duration_seconds,
            cycle_interval_seconds=cycle_interval_seconds,
        )
    if normal.startswith("R3-12"):
        return await _r3_dut_reboot(
            runner,
            reproduction_session_id=reproduction_session_id,
            device=device,
            worker_id=worker_id,
            gate_id=gate_id,
            transport=transport,
            duration_seconds=duration_seconds,
            cycle_interval_seconds=cycle_interval_seconds,
        )
    return None
