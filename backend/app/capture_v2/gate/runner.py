from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.capture_v2.bridge import CaptureV2ABBridge
from app.capture_v2.c_bridge import CaptureV2CBridge
from app.capture_v2.errors import CaptureV2Error
from app.capture_v2.gate.advanced import maybe_run_ownership_scenario, maybe_run_segment_scenario
from app.capture_v2.gate.evidence import GateEvidenceCollector
from app.capture_v2.gate.evaluator import GateEvaluator
from app.capture_v2.gate.faults import (
    FaultInjectingAdapter,
    FaultInjectingPersister,
    FaultInjectingStore,
    GateFaultPlan,
    GateSimulatedWorkerCrash,
)
from app.capture_v2.gate.missing_r3 import maybe_run_missing_r3_scenario
from app.capture_v2.gate.models import GateCaseResult, GateRunPaths
from app.capture_v2.gate.reboot_resume import maybe_run_reboot_resume_ownership_scenario
from app.capture_v2.lease.manager import CaptureLeaseManager


class GateRunner:
    def __init__(self, *, session_factory, adapter, profile_root: Path, requested_profile_id: str,
                 object_root: Path | None = None, repo_root: Path | None = None,
                 output_root: Path = Path("/tmp/capture-v2-gates")):
        self.session_factory = session_factory
        self.adapter = adapter
        self.profile_root = Path(profile_root)
        self.requested_profile_id = requested_profile_id
        self.object_root = Path(object_root) if object_root else None
        self.repo_root = Path(repo_root) if repo_root else None
        self.output_root = Path(output_root)

    async def _collector(self, adapter=None) -> GateEvidenceCollector:
        return GateEvidenceCollector(
            session_factory=self.session_factory,
            adapter=adapter or self.adapter,
            object_root=self.object_root,
            repo_root=self.repo_root,
        )

    async def ownership_establish(self, *, reproduction_session_id: str, device: Any,
                                  worker_id: str, gate_id: str = "R2-ESTABLISH") -> tuple[GateCaseResult, dict[str, Any]]:
        special = await maybe_run_reboot_resume_ownership_scenario(
            self,
            reproduction_session_id=reproduction_session_id,
            device=device,
            worker_id=worker_id,
            gate_id=gate_id,
        )
        if special is None:
            special = await maybe_run_ownership_scenario(
                self,
                reproduction_session_id=reproduction_session_id,
                device=device,
                worker_id=worker_id,
                gate_id=gate_id,
            )
        if special is not None:
            return special

        bridge = CaptureV2ABBridge(
            session_factory=self.session_factory,
            adapter=self.adapter,
            profile_root=self.profile_root,
            requested_profile_id=self.requested_profile_id,
        )
        result = await bridge.establish(
            reproduction_session_id=reproduction_session_id,
            device=device,
            worker_id=worker_id,
        )
        facts = {
            "capture_session_id": result.capture_session_id,
            "lease_epoch": result.ownership.lease.lease_epoch,
            "capture_epoch_id": result.ownership.capture_epoch_id,
            "capture_epoch_token": result.ownership.capture_epoch_token,
            "producer_pid": result.ownership.producer.pid,
            "producer_starttime": result.ownership.producer.process_starttime,
            "recovery_status": result.ownership.recovery.status.value,
        }
        paths = GateRunPaths.create(self.output_root, gate_id, device.id)
        collector = await self._collector()
        await collector.collect(
            paths=paths, gate_id=gate_id, capture_session_id=result.capture_session_id,
            device_id=device.id, facts=facts,
        )
        evaluated = GateEvaluator(paths.case_dir).evaluate(gate_id)
        return evaluated, facts

    async def ownership_adopt(self, *, reproduction_session_id: str, device: Any, worker_id: str,
                              before_state: dict[str, Any], gate_id: str = "R2-01") -> GateCaseResult:
        bridge = CaptureV2ABBridge(
            session_factory=self.session_factory, adapter=self.adapter,
            profile_root=self.profile_root, requested_profile_id=self.requested_profile_id,
        )
        result = await bridge.establish(
            reproduction_session_id=reproduction_session_id, device=device, worker_id=worker_id,
        )
        facts = {
            "before_pid": before_state.get("producer_pid"),
            "before_starttime": before_state.get("producer_starttime"),
            "before_capture_epoch_id": before_state.get("capture_epoch_id"),
            "before_lease_epoch": before_state.get("lease_epoch"),
            "after_pid": result.ownership.producer.pid,
            "after_starttime": result.ownership.producer.process_starttime,
            "after_capture_epoch_id": result.ownership.capture_epoch_id,
            "after_lease_epoch": result.ownership.lease.lease_epoch,
            "recovery_status": result.ownership.recovery.status.value,
        }
        paths = GateRunPaths.create(self.output_root, gate_id, device.id)
        collector = await self._collector()
        await collector.collect(
            paths=paths, gate_id=gate_id, capture_session_id=result.capture_session_id,
            device_id=device.id, facts=facts,
        )
        return GateEvaluator(paths.case_dir).evaluate(gate_id)

    async def segment_normal(self, *, reproduction_session_id: str, device: Any, worker_id: str,
                             duration_seconds: float, cycle_interval_seconds: float = 0.5,
                             gate_id: str = "R3-01", fault_plan: GateFaultPlan | None = None,
                             transport: str = "sftp") -> GateCaseResult:
        adapter = self.adapter
        plan = fault_plan or GateFaultPlan()

        special = await maybe_run_missing_r3_scenario(
            self,
            reproduction_session_id=reproduction_session_id,
            device=device,
            worker_id=worker_id,
            gate_id=gate_id,
            plan=plan,
            transport=transport,
            duration_seconds=duration_seconds,
            cycle_interval_seconds=cycle_interval_seconds,
        )
        if special is None:
            special = await maybe_run_segment_scenario(
                self,
                reproduction_session_id=reproduction_session_id,
                device=device,
                worker_id=worker_id,
                gate_id=gate_id,
                plan=plan,
                transport=transport,
                duration_seconds=duration_seconds,
                cycle_interval_seconds=cycle_interval_seconds,
            )
        if special is not None:
            return special

        if plan.sftp_fail_before_get_count or plan.sftp_fail_after_get_count:
            adapter = FaultInjectingAdapter(adapter, plan)
        bridge = CaptureV2CBridge(
            session_factory=self.session_factory,
            adapter=adapter,
            profile_root=self.profile_root,
            requested_profile_id=self.requested_profile_id,
            transport=transport,
        )
        session = await bridge.establish(
            reproduction_session_id=reproduction_session_id,
            device=device,
            worker_id=worker_id,
        )
        # Gate-only server-store failure injection: replace both Pump and Reconciler
        # store references without touching the production factory.
        if plan.persist_fail_count:
            wrapped = FaultInjectingStore(session.components["store"], plan)
            session.components["store"] = wrapped
            session.components["persister"].store = wrapped
            session.components["reconciler"].store = wrapped
            session.components["reconciler"].persister.store = wrapped

        cycles: list[Any] = []
        crash_facts: dict[str, Any] = {}
        if plan.mode == "PERSISTED_BEFORE_ACK" and plan.persist_fail_count:
            # The Gate persister raises a BaseException only after the real
            # SegmentPersister has committed PERSISTED. Pump cannot convert that
            # crash into ERROR. Exiting the session stops only lease renewal and
            # deliberately leaves the producer alive, matching a real worker crash.
            wrapped_persister = FaultInjectingPersister(session.components["persister"], plan)
            session.components["persister"] = wrapped_persister
            session.components["pump"].persister = wrapped_persister
            before_pid = session.bootstrap.ownership.producer.pid
            before_starttime = session.bootstrap.ownership.producer.process_starttime
            before_epoch = session.bootstrap.ownership.capture_epoch_id
            before_lease = session.token.lease_epoch
            try:
                async with session:
                    cycles.extend(await session.drain_for(
                        duration_seconds=duration_seconds,
                        cycle_interval_seconds=cycle_interval_seconds,
                    ))
            except GateSimulatedWorkerCrash as exc:
                crash_facts = {
                    "fault_mode": exc.phase,
                    "fault_segment_id": exc.segment_id,
                    "before_pid": before_pid,
                    "before_starttime": before_starttime,
                    "before_capture_epoch_id": before_epoch,
                    "before_lease_epoch": before_lease,
                }
            else:
                raise CaptureV2Error("GATE_PERSISTED_BEFORE_ACK_FAULT_NOT_TRIGGERED")

            expires = session.token.expires_at
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            wait_seconds = max(0.0, (expires - datetime.now(timezone.utc)).total_seconds()) + 0.5
            await asyncio.sleep(wait_seconds)

            recovered = await bridge.establish(
                reproduction_session_id=reproduction_session_id,
                device=device,
                worker_id=f"{worker_id}-recovery",
            )
            async with recovered:
                cycles.extend(await recovered.drain_for(
                    duration_seconds=max(10.0, min(duration_seconds, 30.0)),
                    cycle_interval_seconds=cycle_interval_seconds,
                ))
            crash_facts.update({
                "after_pid": recovered.bootstrap.ownership.producer.pid,
                "after_starttime": recovered.bootstrap.ownership.producer.process_starttime,
                "after_capture_epoch_id": recovered.bootstrap.ownership.capture_epoch_id,
                "after_lease_epoch": recovered.token.lease_epoch,
            })
            session = recovered
        else:
            async with session:
                cycles.extend(await session.drain_for(
                    duration_seconds=duration_seconds,
                    cycle_interval_seconds=cycle_interval_seconds,
                ))

        facts = {
            "capture_session_id": session.bootstrap.capture_session_id,
            "lease_epoch": session.token.lease_epoch,
            "capture_epoch_id": session.bootstrap.ownership.capture_epoch_id,
            "cycles": len(cycles),
            "sealed": sum(x.pump.sealed for x in cycles),
            "transferred": sum(x.pump.transferred for x in cycles),
            "acked": sum(x.pump.acked for x in cycles),
            "deleted": sum(x.pump.deleted for x in cycles),
            "errors": sum(x.pump.errors for x in cycles),
            "control_authority": session.control_authority,
            **crash_facts,
        }
        paths = GateRunPaths.create(self.output_root, gate_id, device.id)
        collector = await self._collector(adapter=adapter)
        await collector.collect(
            paths=paths, gate_id=gate_id,
            capture_session_id=session.bootstrap.capture_session_id,
            device_id=device.id, facts=facts,
        )
        return GateEvaluator(paths.case_dir).evaluate(gate_id)

    def postgres_lease_race(self, *, device_id: str, capture_session_a: str, capture_session_b: str,
                            worker_a: str = "gate-r1-a", worker_b: str = "gate-r1-b",
                            gate_id: str = "R1-01") -> GateCaseResult:
        import concurrent.futures

        manager = CaptureLeaseManager(self.session_factory, ttl_seconds=30.0)

        def acquire(session_id: str, worker_id: str):
            try:
                token = manager.acquire(
                    device_id=device_id,
                    capture_session_id=session_id,
                    owner_worker_id=worker_id,
                )
                return {"ok": True, "worker": worker_id, "session": session_id, "lease_epoch": token.lease_epoch}
            except CaptureV2Error as exc:
                return {"ok": False, "worker": worker_id, "session": session_id, "code": exc.code, "details": exc.details}

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(acquire, capture_session_a, worker_a), pool.submit(acquire, capture_session_b, worker_b)]
            results = [f.result() for f in futures]
        winners = [r for r in results if r.get("ok")]
        losers = [r for r in results if not r.get("ok")]
        facts = {
            "results": results,
            "winner_count": len(winners),
            "loser_count": len(losers),
            "loser_code": losers[0].get("code") if len(losers) == 1 else None,
        }
        paths = GateRunPaths.create(self.output_root, gate_id, device_id)
        collector = GateEvidenceCollector(
            session_factory=self.session_factory,
            adapter=None,
            object_root=self.object_root,
            repo_root=self.repo_root,
        )
        # Use the winning session when available; lease row is device-scoped.
        session_id = winners[0]["session"] if winners else capture_session_a
        asyncio.run(collector.collect(
            paths=paths, gate_id=gate_id, capture_session_id=session_id,
            device_id=device_id, facts=facts,
        ))
        return GateEvaluator(paths.case_dir).evaluate(gate_id)
