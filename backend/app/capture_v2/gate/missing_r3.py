from __future__ import annotations

import asyncio
import shlex
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from app.capture_v2.c_bridge import CaptureV2CBridge
from app.capture_v2.db_models import CaptureSegment
from app.capture_v2.gate.evaluator import GateEvaluator
from app.capture_v2.gate.evidence import GateEvidenceCollector
from app.capture_v2.gate.faults import FaultInjectingStore, GateFaultPlan
from app.capture_v2.gate.models import GateRunPaths


_PCAP24_PRINTF = "\\324\\303\\262\\241\\002\\000\\004\\000\\000\\000\\000\\000\\000\\000\\000\\000\\377\\377\\000\\000\\001\\000\\000\\000"


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


def _bridge(runner, *, transport: str) -> CaptureV2CBridge:
    return CaptureV2CBridge(
        session_factory=runner.session_factory,
        adapter=runner.adapter,
        profile_root=runner.profile_root,
        requested_profile_id=runner.requested_profile_id,
        transport=transport,
    )


async def _wait_token_expiry(token) -> None:
    expires = token.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    delay = max(0.0, (expires - datetime.now(timezone.utc)).total_seconds()) + 0.5
    await asyncio.sleep(delay)


async def _inject_closed_24b_pcaps(session, *, marker: str, count: int) -> None:
    """Create deterministic closed valid PCAPs under the current fenced epoch.

    The files are Gate-only preconditions. They are never opened by tcpdump, so
    production SegmentSealer sees them as immutable closed files and must move
    them into spool through the normal fenced path.
    """
    epoch_token = session.bootstrap.ownership.capture_epoch_token
    writes = []
    checks = []
    for idx in range(1, int(count) + 1):
        name = f"{marker}_{idx:02d}.pcap"
        path = f'$ROOT/active/{name}'
        writes.append(f"printf '{_PCAP24_PRINTF}' > \"{path}\"")
        checks.append(f"[ \"$(stat -c %s \"{path}\" 2>/dev/null || echo 0)\" -eq 24 ] || exit 83")
    body = (
        f"ROOT=/tmp/aivoip_capture/epochs/{shlex.quote(epoch_token)}\n"
        "mkdir -p \"$ROOT/active\"\n"
        + "\n".join(writes)
        + "\n"
        + "\n".join(checks)
        + "\n"
    )
    await session.components["mutator"].execute_fenced(session.token, body=body)


async def _r3_silent_24b(runner, *, reproduction_session_id: str, device: Any,
                         worker_id: str, gate_id: str, transport: str,
                         duration_seconds: float, cycle_interval_seconds: float):
    """Inject one real header-only classic PCAP into the DUT epoch and drain it.

    This is Gate-only evidence construction. The file is written inside the real
    active CaptureEpoch under the current fenced authority, then the production
    sealer/transfer/persist/ACK/delete pipeline must process it unchanged.
    """
    session = await _bridge(runner, transport=transport).establish(
        reproduction_session_id=reproduction_session_id,
        device=device,
        worker_id=worker_id,
    )
    marker = f"gate_silent_24b_{uuid4().hex[:10]}"
    await _inject_closed_24b_pcaps(session, marker=marker, count=1)

    async with session:
        cycles = await session.drain_for(
            duration_seconds=max(8.0, min(20.0, duration_seconds)),
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
    }
    return await _collect(
        runner,
        gate_id=gate_id,
        capture_session_id=session.bootstrap.capture_session_id,
        device_id=device.id,
        facts=facts,
    )


async def _r3_pending_spool_restart(runner, *, reproduction_session_id: str,
                                    device: Any, worker_id: str, gate_id: str,
                                    plan: GateFaultPlan, transport: str,
                                    duration_seconds: float,
                                    cycle_interval_seconds: float):
    """Combine deterministic real spool backlog with worker restart/adoption.

    First authority keeps the durable store failing while four valid closed PCAPs
    are injected under the current fenced CaptureEpoch. The production sealer must
    turn those files into immutable DUT spool segments that remain unacked. The
    authority then exits without stopping tcpdump. After lease expiry a new
    authority must ADOPT the exact producer/CaptureEpoch and drain the exact
    pre-restart backlog through durable ACK and remote deletion.
    """
    bridge = _bridge(runner, transport=transport)
    first = await bridge.establish(
        reproduction_session_id=reproduction_session_id,
        device=device,
        worker_id=f"{worker_id}-before-restart",
    )
    if plan.persist_fail_count <= 0:
        plan.persist_fail_count = 100000
    wrapped = FaultInjectingStore(first.components["store"], plan)
    first.components["store"] = wrapped
    first.components["persister"].store = wrapped
    first.components["reconciler"].store = wrapped
    first.components["reconciler"].persister.store = wrapped

    phase = max(10.0, min(20.0, duration_seconds / 2.0))
    before_pid = first.bootstrap.ownership.producer.pid
    before_starttime = first.bootstrap.ownership.producer.process_starttime
    before_epoch = first.bootstrap.ownership.capture_epoch_id
    before_lease = first.token.lease_epoch
    injected_count = 4
    marker = f"gate_restart_backlog_{uuid4().hex[:10]}"

    async with first:
        await _inject_closed_24b_pcaps(first, marker=marker, count=injected_count)
        before_cycles = await first.drain_for(
            duration_seconds=phase,
            cycle_interval_seconds=cycle_interval_seconds,
        )
        with runner.session_factory() as db:
            backlog = list(db.query(CaptureSegment).filter(
                CaptureSegment.capture_session_id == first.bootstrap.capture_session_id,
                CaptureSegment.state.notin_(("ACKED", "REMOTE_DELETED")),
            ).order_by(CaptureSegment.segment_seq).all())
            backlog_ids = [row.id for row in backlog]
            backlog_bytes = sum(int(row.remote_size or 0) for row in backlog)
            sample_paths = [row.remote_path for row in backlog[:20]]
            injected_backlog_count = sum(1 for row in backlog if marker in str(row.remote_path or ""))
        remote_sample_exists = 0
        for path in sample_paths:
            text = await first.components["reader"].run(
                f"[ -f {shlex.quote(path)} ] && echo 1 || echo 0"
            )
            if text.strip() == "1":
                remote_sample_exists += 1
        pressure = first.components["pressure"].evaluate(
            capture_session_id=first.bootstrap.capture_session_id,
            max_unacked_bytes=1,
            max_oldest_unacked_seconds=None,
        )

    # __aexit__ intentionally stops only lease renewal. The exact tcpdump producer
    # remains alive. Let the lease expire, then establish a fresh Gate worker.
    await _wait_token_expiry(first.token)
    plan.persist_fail_count = 0
    second = await bridge.establish(
        reproduction_session_id=reproduction_session_id,
        device=device,
        worker_id=f"{worker_id}-after-restart",
    )
    async with second:
        after_cycles = await second.drain_for(
            duration_seconds=phase,
            cycle_interval_seconds=cycle_interval_seconds,
        )

    with runner.session_factory() as db:
        final_rows = list(db.query(CaptureSegment).filter(
            CaptureSegment.id.in_(backlog_ids)
        ).all()) if backlog_ids else []
        recovered_ids = [
            row.id for row in final_rows if row.state == "REMOTE_DELETED"
        ]

    facts = {
        "capture_session_id": second.bootstrap.capture_session_id,
        "injected_closed_segment_count": injected_count,
        "injected_backlog_count": injected_backlog_count,
        "backlog_unacked_count": len(backlog_ids),
        "backlog_unacked_bytes": backlog_bytes,
        "backlog_segment_ids": backlog_ids,
        "backlog_sample_count": len(sample_paths),
        "backlog_remote_sample_exists": remote_sample_exists,
        "pressure_state": pressure.state,
        "pressure_reasons": list(pressure.reasons),
        "before_pid": before_pid,
        "before_starttime": before_starttime,
        "before_capture_epoch_id": before_epoch,
        "before_lease_epoch": before_lease,
        "after_pid": second.bootstrap.ownership.producer.pid,
        "after_starttime": second.bootstrap.ownership.producer.process_starttime,
        "after_capture_epoch_id": second.bootstrap.ownership.capture_epoch_id,
        "after_lease_epoch": second.token.lease_epoch,
        "recovery_status": second.bootstrap.ownership.recovery.status.value,
        "recovery_classification": second.bootstrap.ownership.recovery.classification.value,
        "recovered_backlog_ids": recovered_ids,
        "before_cycles": len(before_cycles),
        "after_cycles": len(after_cycles),
        "after_errors": sum(x.pump.errors for x in after_cycles),
    }
    return await _collect(
        runner,
        gate_id=gate_id,
        capture_session_id=second.bootstrap.capture_session_id,
        device_id=device.id,
        facts=facts,
    )


async def maybe_run_missing_r3_scenario(runner, *, reproduction_session_id: str,
                                        device: Any, worker_id: str, gate_id: str,
                                        plan: GateFaultPlan, transport: str,
                                        duration_seconds: float,
                                        cycle_interval_seconds: float):
    normal = gate_id.upper().replace("_", "-")
    if normal.startswith("R3-09"):
        return await _r3_silent_24b(
            runner,
            reproduction_session_id=reproduction_session_id,
            device=device,
            worker_id=worker_id,
            gate_id=gate_id,
            transport=transport,
            duration_seconds=duration_seconds,
            cycle_interval_seconds=cycle_interval_seconds,
        )
    if normal.startswith("R3-11"):
        return await _r3_pending_spool_restart(
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
    return None
