import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import models as _existing_models  # noqa: F401
from app.db.base import Base
from app.capture_v2.db_models import CaptureEpoch, CaptureEvent, CaptureGap, CaptureSession
from app.capture_v2.enums import CaptureEpochState, CaptureHealth, CaptureSessionState, RecoveryResultStatus
from app.capture_v2.lease.manager import LeaseToken
from app.capture_v2.producer.identity import ProducerIdentity
from app.capture_v2.recovery.manager import RecoveryManager
from app.capture_v2.recovery.models import RecoveryInventory


def _factory():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        engine,
        tables=[CaptureSession.__table__, CaptureEpoch.__table__, CaptureEvent.__table__, CaptureGap.__table__],
    )
    Factory = sessionmaker(bind=engine, expire_on_commit=False)
    with Factory() as db, db.begin():
        db.add(
            CaptureSession(
                id="S1",
                reproduction_session_id="R1",
                device_id="D1",
                state=CaptureSessionState.RECOVERING.value,
                health_status=CaptureHealth.HEALTHY.value,
                capture_profile_id="voip-standard",
                capture_profile_version="2.1.1",
                platform_profile_id="mt7621",
                platform_profile_version="1",
                effective_profile={},
            )
        )
        db.add(
            CaptureEpoch(
                id="E1",
                capture_session_id="S1",
                device_id="D1",
                epoch_index=1,
                epoch_token="CAP1",
                boot_id="BOOT1",
                producer_pid=101,
                producer_starttime=1001,
                producer_cmdline="tcpdump",
                interface="br-lan_400",
                lease_epoch_started=1,
                state=CaptureEpochState.RUNNING.value,
            )
        )
    return Factory


def _token():
    return LeaseToken(
        device_id="D1",
        capture_session_id="S1",
        owner_worker_id="W2",
        lease_epoch=2,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=30),
    )


def _current():
    return ProducerIdentity(
        pid=101,
        process_starttime=1001,
        cmdline="/usr/bin/tcpdump -ni br-lan_400 -w /tmp/aivoip_capture/epochs/CAP1/active/x.pcap",
        interface="br-lan_400",
        output_path="/tmp/aivoip_capture/epochs/CAP1/active/x.pcap",
        capture_epoch="CAP1",
        session_id="S1",
        legacy=False,
    )


def _legacy():
    return ProducerIdentity(
        pid=202,
        process_starttime=2002,
        cmdline="/usr/bin/tcpdump -ni br-lan_400 -w /tmp/aiVoip_ring_old/x.pcap",
        interface="br-lan_400",
        output_path="/tmp/aiVoip_ring_old/x.pcap",
        capture_epoch=None,
        session_id=None,
        legacy=True,
    )


class SequenceScanner:
    def __init__(self, inventories):
        self.inventories = list(inventories)

    async def scan(self):
        if len(self.inventories) > 1:
            return self.inventories.pop(0)
        return self.inventories[0]


class FakeProducerManager:
    def __init__(self):
        self.stopped = []
        self.adopted = []

    async def stop_identity(self, token, producer):
        self.stopped.append(producer)

    async def adopt(self, producer):
        self.adopted.append(producer)
        return producer


def test_multiple_producer_recovery_keeps_exact_current_and_stops_legacy_without_gap():
    current, legacy = _current(), _legacy()
    first = RecoveryInventory(
        boot_id="BOOT1",
        control_lease_epoch=2,
        control_session_id="S1",
        control_boot_id="BOOT1",
        v2_producers=(current,),
        legacy_producers=(legacy,),
    )
    final = RecoveryInventory(
        boot_id="BOOT1",
        control_lease_epoch=2,
        control_session_id="S1",
        control_boot_id="BOOT1",
        v2_producers=(current,),
    )
    pm = FakeProducerManager()
    mgr = RecoveryManager(session_factory=_factory(), scanner=SequenceScanner([first, final]), producer_manager=pm)
    result = asyncio.run(mgr.recover(token=_token()))
    assert result.status == RecoveryResultStatus.CONFLICT_RESOLVED
    assert result.producer == current
    assert pm.stopped == [legacy]
    assert result.gaps_created == ()
    # Stopping an orphan is a destructive ownership action and must be auditable.
    Factory = mgr.session_factory
    with Factory() as db:
        assert any(e.event_type == "PRODUCER_STOPPED" for e in db.query(CaptureEvent).all())


def test_missing_active_producer_records_permanent_auditable_gap_and_fails_epoch():
    inv = RecoveryInventory(
        boot_id="BOOT1",
        control_lease_epoch=2,
        control_session_id="S1",
        control_boot_id="BOOT1",
    )
    Factory = _factory()
    mgr = RecoveryManager(session_factory=Factory, scanner=SequenceScanner([inv]), producer_manager=FakeProducerManager())
    result = asyncio.run(mgr.recover(token=_token()))
    assert result.status == RecoveryResultStatus.REPAIRED
    assert len(result.gaps_created) == 1

    with Factory() as db:
        epoch = db.get(CaptureEpoch, "E1")
        assert epoch.state == CaptureEpochState.FAILED.value
        gaps = db.query(CaptureGap).all()
        assert len(gaps) == 1
        assert gaps[0].reason_code == "PCAP_PRODUCER_GAP"
        assert gaps[0].certainty == "POSSIBLE"
        assert gaps[0].gap_start_ts is None, "recovery detection time must not fake producer death time"
        assert gaps[0].details["gap_start_boundary"] == "UNKNOWN_AT_RECOVERY"
        events = db.query(CaptureEvent).all()
        assert any(e.event_type == "CAPTURE_GAP_START" for e in events)


def test_stale_single_producer_with_missing_active_epoch_records_gap_before_restart():
    legacy = _legacy()
    first = RecoveryInventory(
        boot_id="BOOT1",
        control_lease_epoch=2,
        control_session_id="S1",
        control_boot_id="BOOT1",
        legacy_producers=(legacy,),
    )
    final = RecoveryInventory(
        boot_id="BOOT1",
        control_lease_epoch=2,
        control_session_id="S1",
        control_boot_id="BOOT1",
    )
    Factory = _factory()
    pm = FakeProducerManager()
    mgr = RecoveryManager(session_factory=Factory, scanner=SequenceScanner([first, final]), producer_manager=pm)
    result = asyncio.run(mgr.recover(token=_token()))

    assert result.status == RecoveryResultStatus.REPAIRED
    assert pm.stopped == [legacy]
    assert len(result.gaps_created) == 1
    with Factory() as db:
        epoch = db.get(CaptureEpoch, "E1")
        assert epoch.state == CaptureEpochState.FAILED.value
        gap = db.query(CaptureGap).one()
        assert gap.reason_code == "PCAP_PRODUCER_GAP"
        assert gap.gap_start_ts is None
        assert gap.details["reason"] == "ACTIVE_EPOCH_PROCESS_MISSING_WITH_STALE_PRODUCER"
