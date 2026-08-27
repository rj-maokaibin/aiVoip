from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

# Register Capture V2 tables (shared Base metadata) before create_all.
import app.capture_v2.db_models  # noqa: F401,E402

from app.capture_v2.errors import CaptureV2Error
from app.contracts.enums import CleanupStatus, LockStatus, ReproductionState
from app.core.config import settings
from app.db.base import Base
from app.db.models import (
    Case,
    CaseDevice,
    CleanupRun,
    DeviceDiagnosticLock,
    ReproductionEventRecord,
    ReproductionSession,
)
from app.integrations.storage import FilesystemObjectStorage
from app.reproduction.capture_pipeline import ReproductionCapturePipeline
from app.reproduction.mock_platform import MockReproductionPlatform
from app.reproduction.orchestrator import ReproductionOrchestrator
from app.reproduction.profile import ReproductionProfileRegistry

PROFILE_ROOT = Path(__file__).resolve().parents[2] / "profiles"
_CAPTURE_TMP = tempfile.TemporaryDirectory(prefix="voip-failclosed-")


def _engine():
    # StaticPool keeps ONE shared in-memory connection so every SessionLocal()
    # opened by the workers/tasks sees the same data.
    eng = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(eng)
    return eng


def _case_device(db: Session, no="FC-1"):
    case = Case(case_no=no, summary="fail closed", status="ANALYZING")
    db.add(case)
    db.flush()
    device = CaseDevice(
        case_id=case.id, ip="198.51.100.10", ssh_port=22, sn=f"SN-{no}", username="root"
    )
    db.add(device)
    db.flush()
    return case, device


def _orch(platform):
    base = Path(_CAPTURE_TMP.name) / uuid4().hex
    pipe = ReproductionCapturePipeline(
        root=base / "capture", storage=FilesystemObjectStorage(base / "objects")
    )
    return ReproductionOrchestrator(
        registry=ReproductionProfileRegistry(PROFILE_ROOT), platform=platform, capture_pipeline=pipe
    )


def _session_events(db: Session, session_id: str) -> list[str]:
    rows = db.query(ReproductionEventRecord).filter(
        ReproductionEventRecord.session_id == session_id
    ).all()
    return [r.event_type for r in rows]


class _PreOwnershipV2Platform(MockReproductionPlatform):
    """V2 platform whose ARM fails BEFORE any capture ownership exists."""

    supports_segmented_ring = True
    uses_capture_v2 = True
    capture_session = None

    def arm(self, **kwargs):
        raise CaptureV2Error("PLATFORM_PROFILE_NOT_FOUND", details={"device_tokens": []})


class _PostOwnershipV2Platform(MockReproductionPlatform):
    """V2 platform whose ARM fails AFTER capture ownership is engaged."""

    supports_segmented_ring = True
    uses_capture_v2 = True
    capture_session = object()  # non-None sentinel: producer/ownership engaged

    def arm(self, **kwargs):
        raise CaptureV2Error("POST_OWNERSHIP_FAIL", details={"stage": "producer_started"})


def test_pre_ownership_startup_failure_fail_closes_to_arm_failed():
    eng = _engine()
    with Session(eng) as db:
        case, device = _case_device(db)
        orch = _orch(_PreOwnershipV2Platform())
        session = orch.create_session(
            db, case_id=case.id, profile_id="AUDIO_NOISE", device_id=device.id
        )
        assert session.state == ReproductionState.CREATED.value
        orch.start(db, session=session, owner_worker="w1", actor="test")
        # Never silently stays in CREATED.
        assert session.state == ReproductionState.ARM_FAILED.value
        assert session.terminal_reason == "PLATFORM_PROFILE_NOT_FOUND"
        # Pre-ownership: no DUT cleanup was required or run.
        assert session.cleanup_status == CleanupStatus.NOT_REQUIRED.value
        runs = db.query(CleanupRun).filter(CleanupRun.session_id == session.id).all()
        assert len(runs) == 0
        # Events + audit recorded (CREATED -> AUTO_ARMING -> ARM_FAILED).
        types = _session_events(db, session.id)
        assert "START_ARMING" in types and "ARM_FAILED" in types
        # In-transaction lock was released (no active lock left).
        lock = db.scalar(
            select(DeviceDiagnosticLock).where(
                DeviceDiagnosticLock.session_id == session.id
            )
        )
        assert lock is None or lock.status != LockStatus.ACTIVE.value


def test_post_ownership_failure_still_runs_formal_cleanup():
    eng = _engine()
    with Session(eng) as db:
        case, device = _case_device(db, no="FC-2")
        orch = _orch(_PostOwnershipV2Platform())
        session = orch.create_session(
            db, case_id=case.id, profile_id="AUDIO_NOISE", device_id=device.id
        )
        orch.start(db, session=session, owner_worker="w1", actor="test")
        # Never silently stays in CREATED.
        assert session.state != ReproductionState.CREATED.value
        assert session.terminal_reason == "POST_OWNERSHIP_FAIL"
        # Ownership engaged -> formal cleanup/recovery ran to a verified terminal.
        assert session.cleanup_status == CleanupStatus.CLEANUP_VERIFIED.value
        runs = db.query(CleanupRun).filter(CleanupRun.session_id == session.id).all()
        assert len(runs) >= 1


def test_ssh_retry_errors_are_not_fail_closed_in_orchestrator():
    """DeviceConnectionError/DeviceCommandError must propagate so Celery autoretry
    preserves the existing SSH retry semantics instead of failing the session."""

    class _SshErrorPlatform(MockReproductionPlatform):
        supports_segmented_ring = True
        uses_capture_v2 = True
        capture_session = None

        def arm(self, **kwargs):
            from app.collectors.asyncssh_adapter import DeviceConnectionError

            raise DeviceConnectionError("timeout")

    eng = _engine()
    with Session(eng) as db:
        case, device = _case_device(db, no="FC-3")
        orch = _orch(_SshErrorPlatform())
        session = orch.create_session(
            db, case_id=case.id, profile_id="AUDIO_NOISE", device_id=device.id
        )
        from app.collectors.asyncssh_adapter import DeviceConnectionError

        with pytest.raises(DeviceConnectionError):
            orch.start(db, session=session, owner_worker="w1", actor="test")
        # No fail-closed ARM_FAILED transition happened; the error escaped so the
        # Celery task can autoretry the whole ARM.
        assert session.state != ReproductionState.ARM_FAILED.value
        assert "ARM_FAILED" not in _session_events(db, session.id)


def _call(monkeypatch, eng):
    import app.db.session as dbs

    monkeypatch.setattr(dbs, "SessionLocal", lambda: Session(eng))
    import app.workers.reproduction_tasks as rt

    monkeypatch.setattr(rt, "SessionLocal", lambda: Session(eng), raising=False)


def test_start_reproduction_task_fail_closes_on_deterministic_error(monkeypatch):
    from app.workers import reproduction_tasks as rt
    from app.workers.celery_app import celery_app

    monkeypatch.setattr(settings, "app_env", "development")
    monkeypatch.setattr(settings, "reproduction_platform_mode", "mock")
    monkeypatch.setattr(celery_app.conf, "task_always_eager", True)

    eng = _engine()
    with Session(eng) as db:
        _call(monkeypatch, eng)
        case, device = _case_device(db, no="FC-4")
        orch = _orch(_PreOwnershipV2Platform())
        session = orch.create_session(
            db, case_id=case.id, profile_id="AUDIO_NOISE", device_id=device.id
        )
        db.commit()  # commit so the task's own SessionLocal sees the row
        session_id = session.id
        assert session.state == ReproductionState.CREATED.value

        class _FakeOrch:
            def start(self, db, **kwargs):
                raise CaptureV2Error("PLATFORM_PROFILE_NOT_FOUND", details={"device_tokens": []})

        monkeypatch.setattr(
            rt,
            "_build_orchestrator_for",
            lambda row, connect=False, force_legacy_platform=False: (_FakeOrch(), None, (lambda: None)),
        )
        dispatched = {}

        def fake_apply_async(args, queue=None):
            dispatched["args"] = args
            dispatched["queue"] = queue

        monkeypatch.setattr(rt.cleanup_v2_reproduction, "apply_async", fake_apply_async, raising=False)

        result = rt.start_reproduction.apply(args=[session_id], throw=False)
        with pytest.raises(CaptureV2Error):
            result.get()

    # A fresh session observes the fail-closed outcome committed by the task.
    with Session(eng) as db:
        fresh = db.get(ReproductionSession, session_id)
        assert fresh.state == ReproductionState.ARM_FAILED.value
        assert fresh.terminal_reason == "PLATFORM_PROFILE_NOT_FOUND"
        types = _session_events(db, session_id)
        assert "START_ARMING" in types and "ARM_FAILED" in types
        # Pre-ownership -> no cleanup dispatch needed.
        assert "args" not in dispatched


def test_reconcile_fail_closes_stale_created_session(monkeypatch):
    from app.workers import reproduction_tasks as rt

    monkeypatch.setattr(settings, "reproduction_stale_created_seconds", 60.0)
    eng = _engine()
    with Session(eng) as db:
        _call(monkeypatch, eng)
        case, device = _case_device(db, no="FC-5")
        stale = ReproductionSession(
            case_id=case.id,
            device_id=device.id,
            profile_key="VOIP_GENERIC_FULL_CAPTURE",
            profile_version="1.0",
            profile_checksum="chk",
            effective_profile_snapshot={},
            state=ReproductionState.CREATED.value,
            created_at=datetime.now(timezone.utc) - timedelta(seconds=600),
        )
        db.add(stale)
        db.commit()
        stale_id = stale.id

        class FakeReconciler:
            def reconcile_expired_leases(self, db, exclude_session_ids=None):
                return 0

            def retry_failed_cleanups(self, db, exclude_session_ids=None):
                return 0

        monkeypatch.setattr("app.workers.reproduction_tasks.RecoveryReconciler", FakeReconciler)
        monkeypatch.setattr(
            "app.workers.reproduction_tasks.ensure_reproduction_diagnosis",
            lambda sid: {"status": "SKIP"},
        )
        result = rt.reconcile_reproduction()
        assert stale_id in result["stale_created_fail_closed"]
        db.expire_all()
        fresh = db.get(ReproductionSession, stale_id)
        assert fresh.state == ReproductionState.ARM_FAILED.value
        assert fresh.terminal_reason == "STALE_CREATED_NO_PROGRESS"


def _seed_created(db, *, minutes_old, has_event=False, case_no="FC-6", sn="SN-6"):
    case = Case(case_no=case_no, summary="young", status="ANALYZING")
    db.add(case)
    db.flush()
    device = CaseDevice(case_id=case.id, ip="198.51.100.11", ssh_port=22, sn=sn, username="root")
    db.add(device)
    db.flush()
    session = ReproductionSession(
        case_id=case.id,
        device_id=device.id,
        profile_key="VOIP_GENERIC_FULL_CAPTURE",
        profile_version="1.0",
        profile_checksum="chk",
        effective_profile_snapshot={},
        state=ReproductionState.CREATED.value,
        created_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_old),
    )
    db.add(session)
    db.flush()
    if has_event:
        db.add(
            ReproductionEventRecord(
                session_id=session.id,
                case_id=case.id,
                event_type="START_ARMING",
                source="SYSTEM",
            )
        )
    db.commit()
    return session


def test_reconcile_skips_young_and_progressing_created(monkeypatch):
    from app.workers import reproduction_tasks as rt

    monkeypatch.setattr(settings, "reproduction_stale_created_seconds", 60.0)
    eng = _engine()
    with Session(eng) as db:
        _call(monkeypatch, eng)
        young = _seed_created(db, minutes_old=0, case_no="FC-7", sn="SN-7")
        progressing = _seed_created(db, minutes_old=600, has_event=True, case_no="FC-8", sn="SN-8")

        class FakeReconciler:
            def reconcile_expired_leases(self, db, exclude_session_ids=None):
                return 0

            def retry_failed_cleanups(self, db, exclude_session_ids=None):
                return 0

        monkeypatch.setattr("app.workers.reproduction_tasks.RecoveryReconciler", FakeReconciler)
        monkeypatch.setattr(
            "app.workers.reproduction_tasks.ensure_reproduction_diagnosis",
            lambda sid: {"status": "SKIP"},
        )
        result = rt.reconcile_reproduction()
        assert young.id not in result["stale_created_fail_closed"]
        assert progressing.id not in result["stale_created_fail_closed"]
        db.expire_all()
        assert db.get(ReproductionSession, young.id).state == ReproductionState.CREATED.value
        assert db.get(ReproductionSession, progressing.id).state == ReproductionState.CREATED.value
