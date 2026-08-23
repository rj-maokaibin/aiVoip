import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import models as _existing_models  # noqa: F401
from app.db.base import Base
from app.capture_v2.db_models import CaptureEpoch, CaptureEvent, CaptureGap, CaptureLease, CaptureSession
from app.capture_v2.enums import CaptureSessionState, RecoveryClassification, RecoveryResultStatus
from app.capture_v2.lease.manager import LeaseToken
from app.capture_v2.producer.identity import ProducerIdentity
from app.capture_v2.profiles.schema import EffectiveCaptureProfile
from app.capture_v2.recovery.models import RecoveryResult
from app.capture_v2.supervisor import CaptureSupervisorV2


def _db_factory():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        engine,
        tables=[
            CaptureSession.__table__,
            CaptureLease.__table__,
            CaptureEpoch.__table__,
            CaptureEvent.__table__,
            CaptureGap.__table__,
        ],
    )
    return sessionmaker(bind=engine, expire_on_commit=False)


def _profile():
    return EffectiveCaptureProfile(
        capture_profile_id="voip-standard",
        capture_profile_version="2.1.1",
        platform_profile_id="mt7621",
        platform_profile_version="1",
        resolved={"capture": {"segment_seconds": 5}},
        checksum_sha256="a" * 64,
    )


def _producer():
    return ProducerIdentity(
        pid=101,
        process_starttime=1001,
        cmdline="/usr/bin/tcpdump -ni br-lan_400 -s 0 -U -G 5 -w /tmp/aivoip_capture/epochs/CAP_X/active/capture.pcap",
        interface="br-lan_400",
        output_path="/tmp/aivoip_capture/epochs/CAP_X/active/capture.pcap",
        capture_epoch="CAP_X",
        session_id=None,
        legacy=False,
    )


class FakeLeaseManager:
    def __init__(self):
        self.epoch = 0
        self.releases = []

    def acquire(self, *, device_id, capture_session_id, owner_worker_id):
        self.epoch += 1
        return LeaseToken(
            device_id=device_id,
            capture_session_id=capture_session_id,
            owner_worker_id=owner_worker_id,
            lease_epoch=self.epoch,
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=30),
        )

    def release(self, token):
        self.releases.append(token)


class FakeReader:
    async def boot_id(self):
        return "BOOT1"


class FakeMutator:
    def __init__(self):
        self.published = []

    async def publish_fence(self, token, *, boot_id):
        self.published.append((token.lease_epoch, boot_id))


class FakeRecoveryManager:
    def __init__(self):
        self.calls = 0
        self.adopted = None

    async def recover(self, *, token):
        self.calls += 1
        if self.calls == 1:
            return RecoveryResult(RecoveryResultStatus.CLEAN, RecoveryClassification.CLEAN)
        return RecoveryResult(
            RecoveryResultStatus.ADOPTED,
            RecoveryClassification.SAME_SESSION_ALIVE,
            producer=self.adopted,
        )


class FakeProducerManager:
    def __init__(self, recovery):
        self.current = None
        self.recovery = recovery
        self.starts = 0

    async def start(self, token, spec):
        self.starts += 1
        self.current = ProducerIdentity(
            pid=101,
            process_starttime=1001,
            cmdline=f"/usr/bin/tcpdump -ni {spec.interface} -s 0 -U -G 5 -w /tmp/aivoip_capture/epochs/{spec.capture_epoch}/active/capture.pcap",
            interface=spec.interface,
            output_path=f"/tmp/aivoip_capture/epochs/{spec.capture_epoch}/active/capture.pcap",
            capture_epoch=spec.capture_epoch,
            session_id=spec.session_id,
            legacy=False,
        )
        self.recovery.adopted = self.current
        return self.current

    async def inspect_owned(self):
        return [self.current] if self.current else []


def test_worker_takeover_reuses_same_capture_session_and_restores_preparing_state():
    Factory = _db_factory()
    lease = FakeLeaseManager()
    recovery = FakeRecoveryManager()
    producer = FakeProducerManager(recovery)
    supervisor = CaptureSupervisorV2(
        session_factory=Factory,
        lease_manager=lease,
        reader=FakeReader(),
        mutator=FakeMutator(),
        recovery_manager=recovery,
        producer_manager=producer,
    )
    capture_session_id = supervisor.create_session(
        reproduction_session_id="R1",
        device_id="D1",
        effective_profile=_profile(),
    )

    first = asyncio.run(
        supervisor.establish_ownership(
            capture_session_id=capture_session_id,
            device_id="D1",
            worker_id="W1",
            voice_interface="br-lan_400",
        )
    )
    assert producer.starts == 1
    first_epoch = first.capture_epoch_token

    with Factory() as db:
        assert db.get(CaptureSession, capture_session_id).state == CaptureSessionState.PREPARING.value

    # Simulate Worker A disappearing while tcpdump remains alive. Worker B enters
    # the same CaptureSession and Recovery reports the existing producer for adopt.
    second = asyncio.run(
        supervisor.establish_ownership(
            capture_session_id=capture_session_id,
            device_id="D1",
            worker_id="W2",
            voice_interface="br-lan_400",
        )
    )
    assert producer.starts == 1, "takeover must not restart a healthy producer"
    assert second.capture_epoch_token == first_epoch
    assert second.producer.pid == first.producer.pid
    assert second.lease.lease_epoch == first.lease.lease_epoch + 1

    with Factory() as db:
        assert db.get(CaptureSession, capture_session_id).state == CaptureSessionState.PREPARING.value
        epochs = db.query(CaptureEpoch).all()
        assert len(epochs) == 1
        events = db.query(CaptureEvent).all()
        fence_events = [e for e in events if e.event_type == "DUT_FENCE_PUBLISHED"]
        assert len(fence_events) == 2
        assert [e.payload["lease_epoch"] for e in fence_events] == [1, 2]


class BusyLeaseManager:
    def acquire(self, **kwargs):
        from app.capture_v2.errors import CaptureV2Error
        raise CaptureV2Error("LEASE_BUSY")

    def release(self, token):
        raise AssertionError("no token was acquired")


def test_lease_busy_contender_does_not_mutate_running_session_state():
    from app.capture_v2.errors import CaptureV2Error

    Factory = _db_factory()
    recovery = FakeRecoveryManager()
    supervisor = CaptureSupervisorV2(
        session_factory=Factory,
        lease_manager=BusyLeaseManager(),
        reader=FakeReader(),
        mutator=FakeMutator(),
        recovery_manager=recovery,
        producer_manager=FakeProducerManager(recovery),
    )
    capture_session_id = supervisor.create_session(
        reproduction_session_id="R_BUSY",
        device_id="D_BUSY",
        effective_profile=_profile(),
    )
    with Factory() as db, db.begin():
        row = db.get(CaptureSession, capture_session_id)
        row.state = CaptureSessionState.WATCHING.value

    import pytest
    with pytest.raises(CaptureV2Error) as exc:
        asyncio.run(
            supervisor.establish_ownership(
                capture_session_id=capture_session_id,
                device_id="D_BUSY",
                worker_id="LOSER",
                voice_interface="br-lan_400",
            )
        )
    assert exc.value.code == "LEASE_BUSY"
    with Factory() as db:
        assert db.get(CaptureSession, capture_session_id).state == CaptureSessionState.WATCHING.value


class FailingRecoveryManager:
    async def recover(self, *, token):
        from app.capture_v2.errors import CaptureV2Error
        raise CaptureV2Error("RECOVERY_FAILED")


def test_failed_recovery_releases_authority_and_never_advances_to_preparing_or_ready():
    from app.capture_v2.errors import CaptureV2Error

    Factory = _db_factory()
    lease = FakeLeaseManager()
    recovery_for_producer = FakeRecoveryManager()
    producer = FakeProducerManager(recovery_for_producer)
    supervisor = CaptureSupervisorV2(
        session_factory=Factory,
        lease_manager=lease,
        reader=FakeReader(),
        mutator=FakeMutator(),
        recovery_manager=FailingRecoveryManager(),
        producer_manager=producer,
    )
    capture_session_id = supervisor.create_session(
        reproduction_session_id="R_FAIL",
        device_id="D_FAIL",
        effective_profile=_profile(),
    )

    with pytest.raises(CaptureV2Error) as exc:
        asyncio.run(
            supervisor.establish_ownership(
                capture_session_id=capture_session_id,
                device_id="D_FAIL",
                worker_id="W_FAIL",
                voice_interface="br-lan_400",
            )
        )
    assert exc.value.code == "RECOVERY_FAILED"
    assert len(lease.releases) == 1
    assert producer.starts == 0
    with Factory() as db:
        row = db.get(CaptureSession, capture_session_id)
        assert row.state == CaptureSessionState.RECOVERING.value
        assert row.state not in {
            CaptureSessionState.PREPARING.value,
            CaptureSessionState.CAPTURE_PATH_READY.value,
        }
