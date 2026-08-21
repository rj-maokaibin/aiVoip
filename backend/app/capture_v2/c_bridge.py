from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from app.capture_v2.bridge import ABOwnershipBootstrapResult, CaptureV2ABBridge
from app.capture_v2.errors import CaptureV2Error
from app.capture_v2.factory import build_capture_v2_c
from app.capture_v2.lease.manager import LeaseToken
from app.capture_v2.segment.pressure import SpoolPressure
from app.capture_v2.transfer.pump import PumpResult


@dataclass(frozen=True)
class CDrainResult:
    pump: PumpResult
    pressure: SpoolPressure
    lease_epoch: int
    control_authority: str = "ACTIVE"


class CaptureV2CGateSession:
    """Phase-C gate runtime over one A/B-owned continuous CaptureEpoch.

    This object deliberately does not own producer shutdown. Losing the lease,
    stopping the renewer, or crashing this process must not stop tcpdump. A later
    worker acquires a higher lease_epoch and adopts/reconciles the producer and
    spool through Phase-B recovery.
    """

    def __init__(
        self,
        *,
        bootstrap: ABOwnershipBootstrapResult,
        components: dict[str, Any],
        renew_interval_seconds: float,
    ):
        self.bootstrap = bootstrap
        self.components = components
        self.renew_interval_seconds = float(renew_interval_seconds)
        if self.renew_interval_seconds <= 0:
            raise ValueError("CAPTURE_LEASE_RENEW_INTERVAL_INVALID")
        self._token: LeaseToken = bootstrap.ownership.lease
        self._renewer: asyncio.Task | None = None
        self._stop_renewer = asyncio.Event()
        self._control_error: CaptureV2Error | None = None

    @property
    def token(self) -> LeaseToken:
        return self._token

    @property
    def control_authority(self) -> str:
        return "LOST" if self._control_error is not None else "ACTIVE"

    def _current_token(self) -> LeaseToken:
        # Pump calls this immediately before fenced ACK/Delete so a long SFTP GET
        # never uses an old LeaseToken whose local expires_at predates a successful
        # background renewal of the same lease_epoch.
        return self._token

    def _raise_if_control_lost(self) -> None:
        if self._control_error is not None:
            raise CaptureV2Error(
                "CAPTURE_CONTROL_AUTHORITY_LOST",
                details={
                    "cause": self._control_error.code,
                    "lease_epoch": self._token.lease_epoch,
                },
            )

    async def _renew_loop(self) -> None:
        try:
            while not self._stop_renewer.is_set():
                try:
                    await asyncio.wait_for(
                        self._stop_renewer.wait(), timeout=self.renew_interval_seconds
                    )
                    break
                except asyncio.TimeoutError:
                    pass
                try:
                    # SQLAlchemy SessionLocal is synchronous; keep DB renewal off
                    # the event loop so SFTP/discovery can continue independently.
                    self._token = await asyncio.to_thread(
                        self.components["lease"].renew, self._token
                    )
                except CaptureV2Error as exc:
                    self._control_error = exc
                    break
                except Exception as exc:  # fail closed for unknown renew failures
                    self._control_error = CaptureV2Error(
                        "LEASE_RENEW_FAILED",
                        details={"exception": type(exc).__name__},
                    )
                    break
        finally:
            # Intentionally no producer stop here. Lease loss is a control-plane
            # degradation, not a capture-plane stop condition.
            return

    async def start_lease_renewer(self) -> None:
        if self._renewer is not None and not self._renewer.done():
            return
        self._stop_renewer.clear()
        self._renewer = asyncio.create_task(self._renew_loop(), name="capture-v2-lease-renewer")

    async def stop_lease_renewer(self, *, release_lease: bool = False) -> None:
        self._stop_renewer.set()
        task = self._renewer
        self._renewer = None
        if task is not None:
            try:
                await task
            except asyncio.CancelledError:
                pass
        if release_lease and self._control_error is None:
            # Releasing authority also intentionally leaves the producer running.
            # The next controller will acquire a higher epoch and ADOPT it.
            await asyncio.to_thread(self.components["lease"].release, self._token)

    async def __aenter__(self) -> "CaptureV2CGateSession":
        await self.start_lease_renewer()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        # A Gate context going away simulates a worker exit. Do not release/stop
        # capture automatically; the TTL/recovery path is part of the reliability
        # contract and avoids an accidental capture gap during process failure.
        await self.stop_lease_renewer(release_lease=False)

    def _pressure_limits(self) -> tuple[int | None, float | None]:
        resolved = self.bootstrap.effective_profile.resolved or {}
        resource = dict(resolved.get("platform_resource") or {})
        spool = dict(resolved.get("spool") or {})
        max_bytes = resource.get("spool_max_unacked_bytes")
        max_age = spool.get("max_oldest_unacked_seconds")
        return (
            int(max_bytes) if max_bytes is not None else None,
            float(max_age) if max_age is not None else None,
        )

    async def reconcile_known_segments(self) -> dict[str, int]:
        """Reconcile every known Segment before/after a Gate worker restart."""
        from app.capture_v2.db_models import CaptureSegment

        with self.components["reconciler"].session_factory() as db:
            ids = [
                row.id
                for row in db.query(CaptureSegment)
                .filter(
                    CaptureSegment.capture_session_id == self.bootstrap.capture_session_id
                )
                .all()
            ]
        counts: dict[str, int] = {}
        for segment_id in ids:
            status = await self.components["reconciler"].reconcile_segment(segment_id)
            counts[status] = counts.get(status, 0) + 1
        return counts

    async def drain_once(self) -> CDrainResult:
        self._raise_if_control_lost()
        # Server-side validation catches a fenced/timed-out token before a DUT
        # mutation. DUT lease_epoch remains the final authority check.
        await asyncio.to_thread(self.components["lease"].validate, self._token)
        self._raise_if_control_lost()

        owned = self.bootstrap.ownership
        result = await self.components["pump"].run_once(
            capture_epoch_id=owned.capture_epoch_id,
            token=self._token,
            token_provider=self._current_token,
            producer_pid=owned.producer.pid,
            producer_starttime=owned.producer.process_starttime,
        )
        self._raise_if_control_lost()
        max_bytes, max_age = self._pressure_limits()
        pressure = self.components["pressure"].evaluate(
            capture_session_id=self.bootstrap.capture_session_id,
            max_unacked_bytes=max_bytes,
            max_oldest_unacked_seconds=max_age,
        )
        return CDrainResult(
            pump=result,
            pressure=pressure,
            lease_epoch=self._token.lease_epoch,
        )

    async def drain_for(
        self,
        *,
        duration_seconds: float,
        cycle_interval_seconds: float = 0.5,
    ) -> tuple[CDrainResult, ...]:
        if duration_seconds < 0 or cycle_interval_seconds <= 0:
            raise ValueError("CAPTURE_C_DRAIN_INTERVAL_INVALID")
        await self.start_lease_renewer()
        deadline = time.monotonic() + float(duration_seconds)
        results: list[CDrainResult] = []
        while True:
            self._raise_if_control_lost()
            results.append(await self.drain_once())
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(min(cycle_interval_seconds, max(0.0, deadline - time.monotonic())))
        return tuple(results)


class CaptureV2CBridge:
    """Non-production Phase-C bridge used by APF1250/APF3260-M C-Gates.

    It composes the proven Phase-A/B ownership bootstrap with the reliable
    Segment/SFTP/ACK pipeline. It intentionally does not advertise Stage-1
    CAPTURE_PATH_READY and does not enable the normal V2 production watcher;
    Phase-D still owns readiness and live orchestration activation.
    """

    def __init__(
        self,
        *,
        session_factory: Callable,
        adapter: Any,
        profile_root: Path,
        requested_profile_id: str,
        transport: str = "sftp",
    ):
        self.session_factory = session_factory
        self.adapter = adapter
        self.profile_root = Path(profile_root)
        self.requested_profile_id = requested_profile_id
        self.transport = transport

    async def establish(
        self,
        *,
        reproduction_session_id: str,
        device: Any,
        worker_id: str,
    ) -> CaptureV2CGateSession:
        # CaptureV2ABBridge currently uses the application's SessionLocal through
        # its factory composition. The explicit session_factory here is retained
        # for the C-Gate contract and tests; production composition uses SessionLocal.
        ab = CaptureV2ABBridge(
            session_factory=self.session_factory,
            adapter=self.adapter,
            profile_root=self.profile_root,
            requested_profile_id=self.requested_profile_id,
        )
        bootstrap = await ab.establish(
            reproduction_session_id=reproduction_session_id,
            device=device,
            worker_id=worker_id,
        )
        components = build_capture_v2_c(
            adapter=self.adapter,
            effective_profile=bootstrap.effective_profile,
            transport=self.transport,
        )
        lease_cfg = dict((bootstrap.effective_profile.resolved or {}).get("lease") or {})
        renew = float(lease_cfg.get("renew_interval_seconds") or 10.0)
        return CaptureV2CGateSession(
            bootstrap=bootstrap,
            components=components,
            renew_interval_seconds=renew,
        )
