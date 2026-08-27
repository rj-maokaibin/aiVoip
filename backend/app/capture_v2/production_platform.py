from __future__ import annotations

import asyncio
import shlex
from pathlib import Path

from sqlalchemy import select

from app.capture_v2.c_bridge import CaptureV2CBridge, CaptureV2CGateSession
from app.capture_v2.db_models import CaptureSegment, CaptureSession
from app.capture_v2.finalizer import CaptureV2CaptureFinalizer, FinalizeResult
from app.capture_v2.production_readiness import evaluate_production_stage1
from app.core.config import settings
from app.db.models import DeviceDiagnosticLock
from app.db.session import SessionLocal
from app.reproduction.pcap_codec import merge_classic_pcaps
from app.reproduction.real_platform import RealCapture, RealReproductionPlatform, _EventLoopBridge
from app.contracts.enums import ChannelHealth


class CaptureV2ProductionPlatform(RealReproductionPlatform):
    """Real reproduction platform whose only long-lived PCAP authority is V2.

    The mature V1.1 FXS/call business state machine is reused, but START_VOICE_PCAP
    is translated into a real Capture V2 fenced producer before ARM readiness may
    pass.  The short-lived start task then drops only its controller renewer; the
    producer keeps capturing continuously until the reproduction-watch worker
    idempotently adopts/renews the same logical owner.  This closes the historical
    ARM->watcher acquisition gap without ever starting the legacy ring producer.
    """

    platform_id = "ruijie-voip-capture-v2"
    version = "2.1.1"
    supports_segmented_ring = True
    uses_capture_v2 = True

    def __init__(
        self,
        *,
        adapter,
        capture_adapter=None,
        transport: str | None = None,
        reproduction_session_id: str | None = None,
        worker_id: str | None = None,
    ):
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
        self._reproduction_session_id = str(reproduction_session_id) if reproduction_session_id else None
        self._capture_worker_id = str(worker_id) if worker_id else None
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

    @staticmethod
    def _worker_id(session_id: str) -> str:
        configured = str(getattr(settings, "capture_v2_worker_id", "") or "").strip()
        return configured or f"reproduction-watch:{session_id}"

    def _resolve_reproduction_binding(self, device) -> tuple[str, str]:
        if self._reproduction_session_id:
            return self._reproduction_session_id, self._capture_worker_id or self._worker_id(self._reproduction_session_id)
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
        return session_id, self._worker_id(session_id)

    def _has_existing_capture_session(self) -> bool:
        if not self._reproduction_session_id:
            return False
        with SessionLocal() as db:
            return db.scalar(
                select(CaptureSession.id).where(
                    CaptureSession.reproduction_session_id == self._reproduction_session_id
                )
            ) is not None

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

    async def _pcap_readiness_snapshot(self, arm_snapshot: dict) -> dict:
        session = self._capture_session
        if session is None:
            return {
                "status": ChannelHealth.FAILED.value,
                "packet_count": 0,
                "advancing": False,
                "enabled": False,
                "pcap_header_valid": False,
                "capture_path_ready": False,
                "capture_engine": "V2",
                "reason": "CAPTURE_SESSION_NOT_READY",
            }

        # Production activation must use the same real pre-OFFHOOK Stage-1 contract
        # already proven by R4. This persists CaptureSession.path_ready_at and never
        # treats an idle line's lack of business packets as a PCAP-path failure.
        stage1 = await evaluate_production_stage1(
            c_session=session,
            arm_snapshot=arm_snapshot,
            session_factory=SessionLocal,
        )

        # Keep actual PCAP-header observation as diagnostics/data-plane evidence.
        # It is intentionally not the Stage-1 gate because tcpdump may not create a
        # rotated business file until packets arrive on an otherwise healthy path.
        epoch = shlex.quote(session.bootstrap.ownership.capture_epoch_token)
        header_valid = False
        header_size = 0
        header_magic = ""
        for _ in range(10):
            raw = (
                await session.components["reader"].run(
                    "root=/tmp/aivoip_capture/epochs/" + epoch + "/active; "
                    "f=$(ls \"$root\"/*.pcap 2>/dev/null | sort | tail -n 1); "
                    "if [ -n \"$f\" ]; then "
                    "n=$(wc -c < \"$f\" 2>/dev/null || echo 0); "
                    "m=$(od -An -tx1 -N4 \"$f\" 2>/dev/null | tr -d ' \\n'); "
                    "printf '%s %s\\n' \"$n\" \"$m\"; else echo '0'; fi"
                )
            ).strip()
            parts = raw.split()
            try:
                header_size = int(parts[0]) if parts else 0
            except ValueError:
                header_size = 0
            header_magic = parts[1].lower() if len(parts) > 1 else ""
            header_valid = header_size >= 24 and header_magic in {
                "d4c3b2a1", "a1b2c3d4", "4d3cb2a1", "a1b23c4d"
            }
            if header_valid:
                break
            await asyncio.sleep(0.1)

        ready = bool(stage1.get("ready"))
        exact_count = int(stage1.get("exact_producer_count") or 0)
        producer_count = int(stage1.get("producer_count") or 0)
        return {
            "status": ChannelHealth.HEALTHY.value if ready else ChannelHealth.FAILED.value,
            "packet_count": 0,
            "advancing": exact_count == 1,
            "enabled": exact_count == 1,
            "pcap_header_valid": header_valid,
            "capture_path_ready": ready,
            "verification_pending": not header_valid,
            "readiness_phase": "CAPTURE_PATH_READY" if ready else "NOT_READY",
            "capture_engine": "V2",
            "producer_count": producer_count,
            "exact_producer_count": exact_count,
            "lease_epoch": int(session.token.lease_epoch),
            "header_size": header_size,
            "header_magic": header_magic,
            "stage1": stage1,
        }

    def resolve_voice_context(self, device):
        context = super().resolve_voice_context(device)
        try:
            # Watch/recovery runs after the business lock commit and can bind from
            # that authoritative lock exactly as before. During reproduction.start,
            # however, the lock was acquired in the caller's still-open transaction;
            # a second SessionLocal cannot see it yet. Defer only that specific case
            # until arm(), which receives the explicit reproduction session id.
            self._ensure_capture_v2(device)
        except RuntimeError as exc:
            if str(exc) != "CAPTURE_V2_ACTIVE_DEVICE_LOCK_NOT_FOUND":
                raise
            self._capture_device = device
        return context

    def arm(self, *, session_id: str, device, actions: list[str]):
        # START_VOICE_PCAP is deliberately removed before entering the legacy real
        # platform. Its historical implementation starts a separate 3s tcpdump
        # probe; V2 readiness below proves the actual long-lived fenced producer.
        if self._capture_session is None:
            explicit_session_id = str(session_id)
            if self._reproduction_session_id not in {None, explicit_session_id}:
                raise RuntimeError("CAPTURE_V2_REPRODUCTION_BINDING_MISMATCH")
            self._capture_device = device
            self._reproduction_session_id = explicit_session_id
            self._capture_worker_id = self._capture_worker_id or self._worker_id(explicit_session_id)
            self._ensure_capture_v2(device)
        filtered = [action for action in actions if action != "START_VOICE_PCAP"]
        result = super().arm(session_id=session_id, device=device, actions=filtered)
        if "START_VOICE_PCAP" in actions:
            result["PCAP"] = self._capture_bridge.run(self._pcap_readiness_snapshot(result))
        return self._normalize_snapshot(result)

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
            mutator=session.components["mutator"],
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
        # Cleanup-only workers may run after the business device lock expired. An
        # explicit reproduction_session_id lets them safely adopt an existing V2
        # CaptureSession. Never create a brand-new V2 epoch solely to clean up a
        # session which never started V2 capture.
        if self._capture_session is None and not self._capture_finalized:
            if self._reproduction_session_id is None:
                self._capture_device = device
                self._reproduction_session_id, self._capture_worker_id = self._resolve_reproduction_binding(device)
            if self._has_existing_capture_session():
                self._capture_device = device
                if self._capture_worker_id is None:
                    self._capture_worker_id = self._worker_id(self._reproduction_session_id)
                self._capture_bridge.run(self._connect_capture_v2())
        self.finalize_capture_if_active(reason="REPRODUCTION_CLEANUP")
        return super().cleanup(session_id=session_id, device=device, actions=actions)

    async def _disconnect_capture_adapter(self) -> None:
        session = self._capture_session
        if session is not None and not self._capture_finalized:
            # Handoff/crash behavior: stop controller renewal but leave the exact
            # producer running. The next controller adopts it without a gap.
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
