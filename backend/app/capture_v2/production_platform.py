from __future__ import annotations

import asyncio
from pathlib import Path

from sqlalchemy import select

from app.capture_v2.c_bridge import CaptureV2CBridge, CaptureV2CGateSession
from app.capture_v2.db_models import CaptureSegment
from app.capture_v2.finalizer import CaptureV2CaptureFinalizer, FinalizeResult
from app.core.config import settings
from app.db.models import DeviceDiagnosticLock
from app.db.session import SessionLocal
from app.reproduction.pcap_codec import merge_classic_pcaps
from app.reproduction.real_platform import RealCapture, RealReproductionPlatform, _EventLoopBridge


class CaptureV2ProductionPlatform(RealReproductionPlatform):
    """Real reproduction platform whose only long-lived PCAP authority is V2.

    V1's mature FXS/call state machine is intentionally reused. Capture V2 is
    lazy-bound only after the watcher resolves the real device and its ACTIVE
    diagnostic lock, so the short-lived ``reproduction.start`` task can never own
    a V2 lease that becomes orphaned when that task exits.
    """

    platform_id = "ruijie-voip-capture-v2"
    version = "2.1.1"
    supports_segmented_ring = True
    uses_capture_v2 = True

    def __init__(self, *, adapter, capture_adapter=None, transport: str | None = None):
        super().__init__(adapter=adapter)
        if capture_adapter is None:
            from app.collectors.asyncssh_adapter import AsyncSSHDeviceAdapter
            capture_adapter = AsyncSSHDeviceAdapter(
                ip=adapter.ip,
                port=adapter.port,
                username=adapter.username,
                password=adapter.password,
                aim_prompt=adapter.aim_prompt,
                aim_executable=adapter.aim_executable,
                kex_algs=list(adapter.kex_algs),
            )
        self._capture_adapter = capture_adapter
        self._capture_transport = str(
            transport or getattr(settings, "capture_v2_transport", "scp")
        ).lower().strip()
        if self._capture_transport not in {"scp", "sftp"}:
            raise ValueError("CAPTURE_V2_PRODUCTION_TRANSPORT_INVALID")
        self._capture_bridge = _EventLoopBridge()
        self._capture_session: CaptureV2CGateSession | None = None
        self._capture_finalized = False
        self._capture_finalize_result: FinalizeResult | None = None
        self._capture_seen_segments: set[str] = set()
        self._capture_device = None
        self._reproduction_session_id: str | None = None
        self._capture_worker_id: str | None = None
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

    def _resolve_reproduction_binding(self, device) -> tuple[str, str]:
        with SessionLocal() as db:
            lock = db.scalar(
                select(DeviceDiagnosticLock).where(
                    DeviceDiagnosticLock.device_id == device.id,
                    DeviceDiagnosticLock.status == "ACTIVE",
                )
            )
        if lock is None or not lock.session_id:
            raise RuntimeError("CAPTURE_V2_ACTIVE_DEVICE_LOCK_NOT_FOUND")
        session_id = str(lock.session_id)
        configured = str(getattr(settings, "capture_v2_worker_id", "") or "").strip()
        worker_id = configured or f"reproduction-watch:{session_id}"
        return session_id, worker_id

    async def _connect_capture_v2(self) -> None:
        if self._capture_session is not None:
            return
        if self._capture_device is None or not self._reproduction_session_id or not self._capture_worker_id:
            raise RuntimeError("CAPTURE_V2_PRODUCTION_BINDING_NOT_READY")
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
        await session.drain_once()
        self._capture_session = session

    def _ensure_capture_v2(self, device) -> None:
        if self._capture_session is not None:
            return
        self._capture_device = device
        self._reproduction_session_id, self._capture_worker_id = self._resolve_reproduction_binding(device)
        self._capture_bridge.run(self._connect_capture_v2())

    def resolve_voice_context(self, device):
        context = super().resolve_voice_context(device)
        self._ensure_capture_v2(device)
        return context

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
        await asyncio.sleep(max(0.1, float(seconds)))
        await session.drain_once()
        paths, pending = self._durable_segment_paths()
        if not paths:
            return RealCapture(pcap=b"", debug_log=b"", remaining_files=pending)
        self._merge_no += 1
        merge_path = (
            Path(settings.reproduction_capture_root)
            / "capture-v2-production-merge"
            / str(self._reproduction_session_id)
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
        return RealCapture(pcap=payload, debug_log=b"", remaining_files=pending)

    def spawn_ring_segment(self, *, context, seconds: int, segment_key: str):
        del context, segment_key
        return self._capture_bridge.spawn(self._capture_next_segment(int(seconds)))

    def seal_segmented_ring(self, session_id: str | None = None) -> None:
        del session_id

    def stop_segmented_ring(self, session_id: str | None = None) -> None:
        del session_id

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

    def finalize_capture_if_active(
        self, *, reason: str = "REPRODUCTION_WATCHER_EXIT"
    ) -> FinalizeResult | None:
        return self._capture_bridge.run(self._finalize_capture_v2(reason))

    def cleanup(self, *, session_id: str, device, actions: list[str]):
        # A high-priority cancel runs in a different worker from the watcher. The
        # deterministic worker id makes re-acquire idempotent for the same session;
        # bind here if needed so cleanup can never claim success before V2 stops.
        if self._capture_session is None and not self._capture_finalized:
            self._ensure_capture_v2(device)
        self.finalize_capture_if_active(reason="REPRODUCTION_CLEANUP")
        return super().cleanup(session_id=session_id, device=device, actions=actions)

    async def _disconnect_capture_adapter(self) -> None:
        session = self._capture_session
        if session is not None and not self._capture_finalized:
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
