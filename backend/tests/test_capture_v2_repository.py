from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
import pytest

from app.db import models as _existing_models  # noqa: F401
from app.db.base import Base
from app.capture_v2.db_models import CaptureSession
from app.capture_v2.enums import CaptureHealth, CaptureSessionState
from app.capture_v2.errors import CaptureV2Error
from app.capture_v2.repository.core import CaptureSessionRepository


def _factory():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=[CaptureSession.__table__])
    Factory = sessionmaker(bind=engine, expire_on_commit=False)
    with Factory() as db, db.begin():
        db.add(
            CaptureSession(
                id="S1",
                reproduction_session_id="R1",
                device_id="D1",
                state=CaptureSessionState.CREATED.value,
                health_status=CaptureHealth.HEALTHY.value,
                capture_profile_id="voip-standard",
                capture_profile_version="2.1.1",
                platform_profile_id="mt7621",
                platform_profile_version="1",
                effective_profile={},
            )
        )
    return Factory


def test_capture_session_transition_is_compare_and_swap():
    Factory = _factory()
    with Factory() as db, db.begin():
        CaptureSessionRepository(db).transition(
            "S1",
            expected=CaptureSessionState.CREATED.value,
            next_state=CaptureSessionState.ACQUIRING_LEASE.value,
        )

    with Factory() as db, db.begin():
        with pytest.raises(CaptureV2Error) as exc:
            CaptureSessionRepository(db).transition(
                "S1",
                expected=CaptureSessionState.CREATED.value,
                next_state=CaptureSessionState.RECOVERING.value,
            )
        assert exc.value.code == "CAPTURE_STATE_CONFLICT"

    with Factory() as db:
        assert db.get(CaptureSession, "S1").state == CaptureSessionState.ACQUIRING_LEASE.value


def test_capture_epoch_unique_constraints_reject_duplicate_session_index_and_device_token():
    from sqlalchemy.exc import IntegrityError
    from app.capture_v2.db_models import CaptureEpoch

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=[CaptureSession.__table__, CaptureEpoch.__table__])
    Factory = sessionmaker(bind=engine, expire_on_commit=False)
    with Factory() as db, db.begin():
        db.add(
            CaptureSession(
                id="S1", reproduction_session_id="R1", device_id="D1",
                state=CaptureSessionState.CREATED.value, health_status=CaptureHealth.HEALTHY.value,
                capture_profile_id="voip-standard", capture_profile_version="2.1.1",
                platform_profile_id="mt7621", platform_profile_version="1", effective_profile={},
            )
        )
        db.add(
            CaptureEpoch(
                id="E1", capture_session_id="S1", device_id="D1", epoch_index=1,
                epoch_token="CAP_A", boot_id="B1", interface="br-lan_400",
                lease_epoch_started=1, state="RUNNING",
            )
        )

    with Factory() as db:
        db.add(
            CaptureEpoch(
                id="E2", capture_session_id="S1", device_id="D1", epoch_index=1,
                epoch_token="CAP_B", boot_id="B1", interface="br-lan_400",
                lease_epoch_started=2, state="STARTING",
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

    with Factory() as db:
        db.add(
            CaptureEpoch(
                id="E3", capture_session_id="S1", device_id="D1", epoch_index=2,
                epoch_token="CAP_A", boot_id="B1", interface="br-lan_400",
                lease_epoch_started=2, state="STARTING",
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()
