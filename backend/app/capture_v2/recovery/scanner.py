from __future__ import annotations

from app.capture_v2.producer.identity import ProducerIdentity, parse_process_record
from app.capture_v2.recovery.models import RecoveryInventory
from app.capture_v2.transport.readonly import ReadOnlyDeviceTransport


class RecoveryScanner:
    def __init__(self, reader: ReadOnlyDeviceTransport):
        self.reader = reader

    async def scan(self) -> RecoveryInventory:
        boot_id = await self.reader.boot_id()
        control_epoch_raw = await self.reader.read_text(
            "/tmp/aivoip_capture/control/lease_epoch", missing_ok=True
        )
        control_session = await self.reader.read_text(
            "/tmp/aivoip_capture/control/session_id", missing_ok=True
        )
        control_boot = await self.reader.read_text(
            "/tmp/aivoip_capture/control/boot_id", missing_ok=True
        )
        try:
            control_epoch = int(control_epoch_raw) if control_epoch_raw else None
        except ValueError:
            control_epoch = None

        epoch_dirs = tuple(await self.reader.list_epoch_dirs())
        legacy_dirs = tuple(await self.reader.list_legacy_ring_dirs())
        v2: list[ProducerIdentity] = []
        legacy: list[ProducerIdentity] = []
        foreign: list[ProducerIdentity] = []

        for proc in await self.reader.list_tcpdump_processes():
            identity = parse_process_record(proc.pid, proc.starttime, proc.cmdline)
            if identity.capture_epoch:
                session_id = await self.reader.read_text(
                    f"/tmp/aivoip_capture/epochs/{identity.capture_epoch}/session_id", missing_ok=True
                )
                v2.append(identity.with_session(session_id))
            elif identity.legacy:
                legacy.append(identity)
            else:
                foreign.append(identity)

        return RecoveryInventory(
            boot_id=boot_id,
            control_lease_epoch=control_epoch,
            control_session_id=control_session,
            control_boot_id=control_boot,
            v2_producers=tuple(v2),
            legacy_producers=tuple(legacy),
            foreign_tcpdump=tuple(foreign),
            epoch_dirs=epoch_dirs,
            legacy_ring_dirs=legacy_dirs,
        )
