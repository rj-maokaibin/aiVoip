from __future__ import annotations

from app.capture_v2.errors import CaptureV2Error
from app.capture_v2.lease.manager import CaptureLeaseManager
from app.capture_v2.producer.manager import ProducerManager
from app.capture_v2.profiles.schema import EffectiveCaptureProfile
from app.capture_v2.recovery.manager import RecoveryManager
from app.capture_v2.recovery.scanner import RecoveryScanner
from app.capture_v2.supervisor import CaptureSupervisorV2
from app.capture_v2.transport.mutator import FencedDeviceMutator
from app.capture_v2.transport.readonly import ReadOnlyDeviceTransport
from app.core.config import settings
from app.db.session import SessionLocal


def _lease_ttl(*, effective_profile: EffectiveCaptureProfile | None, explicit: float | None) -> float:
    if effective_profile is not None:
        lease = dict((effective_profile.resolved or {}).get("lease") or {})
        value = lease.get("ttl_seconds")
        if value is not None:
            return float(value)
    if explicit is not None:
        return float(explicit)
    return float(settings.capture_v2_lease_ttl_seconds)


def build_capture_v2_ab(
    *,
    adapter,
    effective_profile: EffectiveCaptureProfile | None = None,
    lease_ttl_seconds: float | None = None,
) -> CaptureSupervisorV2:
    reader = ReadOnlyDeviceTransport(adapter)
    mutator = FencedDeviceMutator(adapter, reader)
    producer = ProducerManager(reader, mutator)
    scanner = RecoveryScanner(reader)
    recovery = RecoveryManager(
        session_factory=SessionLocal,
        scanner=scanner,
        producer_manager=producer,
    )
    lease = CaptureLeaseManager(
        SessionLocal,
        ttl_seconds=_lease_ttl(effective_profile=effective_profile, explicit=lease_ttl_seconds),
    )
    return CaptureSupervisorV2(
        session_factory=SessionLocal,
        lease_manager=lease,
        reader=reader,
        mutator=mutator,
        recovery_manager=recovery,
        producer_manager=producer,
    )



def build_capture_v2_c(*, adapter, effective_profile: EffectiveCaptureProfile, transport: str = "sftp") -> dict:
    """Compose Phase-C reliable Segment/SFTP/ACK components.

    This factory is deliberately separate from production activation. It is used by
    C-Gate and later D composition; V1 remains the live authority until cutover gates pass.

    ``transport`` selects the exact-download transport:
    - "sftp" (default): ExactSftpDownloader via the SFTP subsystem.
    - "scp": ExactScpDownloader for platforms whose Dropbear ships no SFTP subsystem.
      Gate-only selection; production composition keeps SFTP as the default.
    """
    from pathlib import Path

    from app.capture_v2.segment.pressure import SpoolPressureEvaluator
    from app.capture_v2.segment.sealer import SegmentSealer
    from app.capture_v2.storage.local import LocalDurableSegmentStore
    from app.capture_v2.transfer.ack import SegmentAcknowledger
    from app.capture_v2.transfer.persister import SegmentPersister
    from app.capture_v2.transfer.pump import ReliableSegmentPump
    from app.capture_v2.transfer.reconciler import SegmentReconciler
    from app.capture_v2.transfer.remote import RemoteSegmentInspector
    from app.capture_v2.transfer.scp import ExactScpDownloader
    from app.capture_v2.transfer.sftp import ExactSftpDownloader

    if transport not in ("sftp", "scp"):
        raise CaptureV2Error("CAPTURE_V2_TRANSPORT_INVALID", details={"transport": transport})

    reader = ReadOnlyDeviceTransport(adapter)
    mutator = FencedDeviceMutator(adapter, reader)
    producer = ProducerManager(reader, mutator)
    # storage_key already carries the "capture-v2/<device>/<epoch>/..." namespace,
    # so the store must be rooted at object_root. Rooting at object_root/"capture-v2"
    # produced a double prefix (capture-v2/capture-v2/...) that the evidence
    # collector (object_root / storage_key) could never resolve, hiding durable
    # server copies from the R3 evaluator.
    store = LocalDurableSegmentStore(Path(settings.reproduction_object_root))
    persister = SegmentPersister(SessionLocal, store)
    sealer = SegmentSealer(reader, mutator)
    inspector = RemoteSegmentInspector(reader)
    downloader = (ExactScpDownloader(adapter) if transport == "scp" else ExactSftpDownloader(adapter))
    acknowledger = SegmentAcknowledger(mutator)
    pump = ReliableSegmentPump(
        session_factory=SessionLocal,
        sealer=sealer,
        inspector=inspector,
        downloader=downloader,
        persister=persister,
        acknowledger=acknowledger,
        temp_root=Path(settings.reproduction_capture_root) / "capture-v2-transfer",
    )
    reconciler = SegmentReconciler(session_factory=SessionLocal, store=store, persister=persister)
    pressure = SpoolPressureEvaluator(SessionLocal)
    lease = CaptureLeaseManager(
        SessionLocal,
        ttl_seconds=_lease_ttl(effective_profile=effective_profile, explicit=None),
    )
    return {
        "reader": reader,
        "mutator": mutator,
        "producer": producer,
        "store": store,
        "persister": persister,
        "sealer": sealer,
        "inspector": inspector,
        "downloader": downloader,
        "acknowledger": acknowledger,
        "pump": pump,
        "reconciler": reconciler,
        "pressure": pressure,
        "lease": lease,
    }
