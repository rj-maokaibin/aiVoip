from __future__ import annotations

import asyncio
from pathlib import Path

from sqlalchemy import select

from app.capture_v2.c_bridge import CaptureV2CBridge, CaptureV2CGateSession
from app.capture_v2.db_models import CaptureSegment
from app.capture_v2.finalizer import CaptureV2CaptureFinalizer, FinalizeResult
from app.core.config import settings
from app.db.session import SessionLocal
from app.reproduction.pcap_codec import merge_classic_pcaps
from app.reproduction.real_platform import RealCapture, RealReproductionPlatform, _EventLoopBridge


class CaptureV2ProductionPlatform(RealReproductionPlatform):
    """Production-compatible Reproduction platform with Capture V2 PCAP authority.

    FXS/PCM/debug control keeps using the proven ``RealReproductionPlatform`` path,
    while the long-lived PCAP producer, segment transfer/ACK and fencing are owned
    exclusively by Capture V2.  The public segmented-ring methods intentionally
    match the V1.1 watcher contract so the mature reproduction state machine can be
    reused without starting a second PCAP producer.

    A second SSH adapter is used for Capture V2.  This is deliberate: the legacy
    real platform owns its adapter on a private bridge loop for AIM/FXS streaming,
    while Capture V2 uses an independent bridge loop for fenced producer and exact
    segment transfer.  No asyncssh connection crosses event loops.
    """

    platform_id = "ruijie-voip-capture-v2"
    version = "2.1.1"
    supports_segmented_ring = True
    uses_capture_v2 = True

    def __init__(
        self,
        *,
        adapter,
        capture_adapter,
        device,
        reproduction_session_id: str,
        worker_id: str,
        transport: str | None = None,
    ):
        super().__init__(adapter=adapter)
        self._capture_adapter = capture_adapter
        self._capture_device = device
        self._reproduction_session_id = str(reproduction_session_id)
        self._capture_worker_id = str(worker_id)
        self._capture_transport = str(transport or settings.capture_v2_transport).lower().strip()
        self._capture_bridge = _EventLoopBridge()
        self._capture_session: CaptureV2CGateSession | None = None
        self._capture_finalized = False
        self._capture_finalize_result: FinalizeResult | None = None
        self._capture_seen_segments: set[str] = set()
        self._tail_drain_requested = False
        self._merge_no = 0

    @property
    def pcm_cleanup_guard(self):
        return self._pcm_guard

    @property
    def capture_session(self) -> CaptureV2CGateSession | None:
        return self._capture_session

    @property
    def capture_finalize_result(self) -> FinalizeResult | None:
        return self._capture_finalize_result

    async def _connect_capture_v2(self) -> None:
        await self._capture_adapter.connect()
        session = await CaptureV2CBridge(
            session_factory=SessionLocal,
            adapter=self._capture_adapter,
            profile_root=Path(settings.profile_root),
            requested_profile_id=str(settings.capture_v2_profile_id),
            transport=self._capture_transport,
        ).establish(
            reproduction_session_id=self._reproduction_session_id,
            device=self._capture_device,
            worker_id=self._capture_worker_id,
        )
        await session.start_lease_renewer()
        # Prove the lease/producer/pump path before the watcher advertises runtime
        # readiness.  An error here fails closed before any V1 ring can be started.
        await session.drain_once()
        self._capture_session = session

    def connect(self):
        super().connect()
        try:
            self._capture_bridge.run(self._connect_capture_v2())
        except Exception:
            try:
                super().disconnect()
            finally:
                raise

    def _durable_segment_paths(self) -> tuple[list[Path], int]:
        session = self._capture_session
        if session is None:
            return [], 0
        epoch_id = session.bootstrap.ownership.capture_epoch_id
        with SessionLocal() as db:
            rows = list(db.scalars(
                select(CaptureSegment).where(
                    CaptureSegment.capture_epoch_id == epoch_id,
                    CaptureSegment.state.in_(("ACKED", "REMOTE_DELETED")),
                ).order_by(CaptureSegment.segment_seq)
            ))
            pending = list(db.scalars(
                select(CaptureSegment).where(
                    CaptureSegment.capture_epoch_id == epoch_id,
                    CaptureSegment.state.not_in(("ACKED", "REMOTE_DELETED")),
                )
            ))
        paths: list[Path] = []
        store_root = Path(session.components["store"].root)
        for row in rows:
            if row.id in self._capture_seen_segments or not row.storage_key:
                continue
            path = store_root / row.storage_key
            if path.is_file():
                paths.append(path)
                self._capture_seen_segments.add(row.id)
        return paths, len(pending)

    async def _capture_next_segment(self, seconds: int) -> RealCapture:
        session = self._capture_session
        if session is None:
            raise RuntimeError("CAPTURE_V2_PRODUCTION_SESSION_NOT_READY")
        # Match the existing segmented watcher cadence.  On an End Anchor this wait
        # is especially important: it lets the currently open V2 file rotate so the
        # post-ONHOOK drain can include the file that actually covers the boundary.
        await asyncio.sleep(max(0.1, float(seconds)))
        await session.drain_once()
        paths, pending = self._durable_segment_paths()
        if not paths:
            return RealCapture(pcap=b"", debug_log=b"", remaining_files=pending)
        self._merge_no += 1
        merge_path = (
            Path(settings.reproduction_capture_root)
            / "capture-v2-production-merge"
            / self._reproduction_session_id
            / f"segment_{self._merge_no:06d}.pcap"
        )
        merge_classic_pcaps(paths, merge_path)
        try:
            payload = merge_path.read_bytes()
        finally:
            try:
                merge_path.unlink(missing_ok=True)
            except Exception:
                pass
        # ``remaining_files`` is the legacy watcher's tail-drain continuation
        # signal.  Capture V2's pump drains every known sealed file in the cycle;
        # any DB row below ACKED therefore correctly requests another drain.
        return RealCapture(pcap=payload, debug_log=b"", remaining_files=pending)

    def spawn_ring_segment(self, *, context, seconds: int, segment_key: str):
        del context, segment_key
        return self._capture_bridge.spawn(self._capture_next_segment(int(seconds)))

    def seal_segmented_ring(self, session_id: str | None = None) -> None:
        # Do not stop the V2 producer at every no-call ONHOOK.  The V2 reliability
        # contract is continuous capture.  The watcher performs an explicit delayed
        # drain after this marker, then clears it through stop_segmented_ring().
        del session_id
        self._tail_drain_requested = True

    def stop_segmented_ring(self, session_id: str | None = None) -> None:
        # Legacy V1.1 uses this to restart an idle producer after a no-call Attempt.
        # V2 must remain continuous, so the operation only resets the compatibility
        # tail marker.  Real producer shutdown happens exclusively in finalization.
        del session_id
        self._tail_drain_requested = False

    async def _finalize_capture_v2(self, reason: str) -> FinalizeResult | None:
        if self._capture_finalized:
            return self._capture_finalize_result
        session = self._capture_session
        if session is None:
            self._capture_finalized = True
            return None
        bootstrap = session.bootstrap
        finalizer = CaptureV2CaptureFinalizer(
            session_factory=SessionLocal,
            producer_manager=session.components["producer"],
            pump=session.components["pump"],
            lease_manager=session.components["lease"],
        )
        result = await finalizer.finalize(
            capture_session_id=bootstrap.capture_session_id,
            capture_epoch_id=bootstrap.ownership.capture_epoch_id,
            capture_epoch_token=bootstrap.ownership.capture_epoch_token,
            producer=bootstrap.ownership.producer,
            token=session.token,
            token_provider=lambda: session.token,
            reason=reason,
        )
        if not result.durable:
            raise RuntimeError("CAPTURE_V2_FINAL_EVIDENCE_NOT_DURABLE")
        await session.stop_lease_renewer(release_lease=True)
        self._capture_finalize_result = result
        self._capture_finalized = True
        return result

    def finalize_capture_if_active(self, *, reason: str = "REPRODUCTION_WATCHER_EXIT") -> FinalizeResult | None:
        return self._capture_bridge.run(self._finalize_capture_v2(reason))

    def cleanup(self, *, session_id: str, device, actions: list[str]):
        # PCAP authority must be truthfully gone and its final segment durable before
        # the existing cleanup barrier is allowed to verify PCM/debug/PCAP shutdown.
        self.finalize_capture_if_active(reason="REPRODUCTION_CLEANUP")
        return super().cleanup(session_id=session_id, device=device, actions=actions)

    async def _disconnect_capture_adapter(self) -> None:
        session = self._capture_session
        if session is not None and not self._capture_finalized:
            # Normal exceptions leave the DUT producer running for the fenced
            # takeover/recovery path, but stop renewing this worker's authority so
            # the lease can expire.  Do not release a possibly incomplete session.
            await session.stop_lease_renewer(release_lease=False)
        try:
            await self._capture_adapter.disconnect()
        except Exception:
            pass

    def disconnect(self):
        try:
            self._capture_bridge.run(self._disconnect_capture_adapter())
        finally:
            super().disconnect()
