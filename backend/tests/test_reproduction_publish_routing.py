from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.capture_v2.db_models  # noqa: F401,E402

from app.core.config import settings
from app.db.base import Base
from app.db.models import Case
from app.schemas.reproduction import ReproductionCreate
from app.workers.celery_app import celery_app

PROFILE_ROOT = Path(__file__).resolve().parents[2] / "profiles"


def _engine():
    eng = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(eng)
    return eng


def test_reproduction_tasks_route_to_their_queues():
    """Routing contract: control/start and cancel land on reproduction-control
    (+high), watcher handoff and V2 cleanup land on reproduction-watch."""
    from app.workers.reproduction_tasks import (
        cleanup_v2_reproduction,
        reconcile_reproduction,
        start_reproduction,
    )

    assert start_reproduction.name == "reproduction.start"
    assert start_reproduction.queue == "reproduction-control"
    assert reconcile_reproduction.name == "reproduction.reconcile"
    assert reconcile_reproduction.queue == "reproduction-control-high"
    assert cleanup_v2_reproduction.name == "reproduction.v2_cleanup"
    assert cleanup_v2_reproduction.queue == "reproduction-watch"
    # The worker must have reproduction.start registered after importing the
    # task modules (inspect registered shows it at runtime).
    assert "reproduction.start" in celery_app.tasks
    assert "reproduction.reconcile" in celery_app.tasks
    # SSH retry semantics are preserved by the task decorator.
    from app.collectors.asyncssh_adapter import DeviceCommandError, DeviceConnectionError

    assert start_reproduction.autoretry_for == (DeviceConnectionError, DeviceCommandError)
    assert start_reproduction.max_retries == 3


def test_create_reproduction_endpoint_publishes_to_control_queue(monkeypatch):
    """POST /cases/{id}/reproductions must publish reproduction.start to the
    reproduction-control queue (the exact contract the reproduction worker
    listens on with `-Q reproduction-control`)."""
    from app.db.models import CaseDevice

    monkeypatch.setattr(settings, "profile_root", PROFILE_ROOT)
    import app.api.v1.reproduction as rep_api

    eng = _engine()
    with Session(eng) as db:
        case = Case(case_no="R-1", summary="routing", status="NEW")
        db.add(case)
        db.flush()
        device = CaseDevice(
            case_id=case.id, ip="198.51.100.30", ssh_port=22, sn="SN-R1", username="root"
        )
        db.add(device)
        db.commit()

        captured = {}

        def fake_apply_async(args, queue=None):
            captured["args"] = list(args)
            captured["queue"] = queue

        monkeypatch.setattr(rep_api.start_reproduction, "apply_async", fake_apply_async, raising=False)

        req = ReproductionCreate(
            profile_id="VOIP_GENERIC_FULL_CAPTURE", symptom_class=None, device_id=device.id
        )
        identity = SimpleNamespace(actor_id="tester")
        row = rep_api.create_reproduction(
            case_id=case.id, req=req, db=db, idempotency_key=None, identity=identity
        )
        assert captured["queue"] == "reproduction-control"
        assert captured["args"] == [row.id]
        assert row.profile_key == "VOIP_GENERIC_FULL_CAPTURE"


def test_start_reproduction_task_successful_execution_persists_watching(monkeypatch):
    """The reproduction.start task executes a real orchestrator start and commits
    the WATCHING state (pre-ownership normal path smoke contract)."""
    import tempfile
    from uuid import uuid4

    from app.contracts.enums import ReproductionState
    from app.db.models import CaseDevice, ReproductionSession
    from app.reproduction.mock_platform import MockReproductionPlatform
    from app.reproduction.orchestrator import ReproductionOrchestrator
    from app.reproduction.profile import ReproductionProfileRegistry
    from app.reproduction.capture_pipeline import ReproductionCapturePipeline
    from app.integrations.storage import FilesystemObjectStorage

    from app.workers import reproduction_tasks as rt

    monkeypatch.setattr(settings, "app_env", "development")
    monkeypatch.setattr(settings, "reproduction_platform_mode", "mock")
    eng = _engine()
    _patch_sessionlocal(monkeypatch, eng)

    with Session(eng) as db:
        case = Case(case_no="R-2", summary="normal", status="ANALYZING")
        db.add(case)
        db.flush()
        device = CaseDevice(
            case_id=case.id, ip="198.51.100.20", ssh_port=22, sn="SN-R2", username="root"
        )
        db.add(device)
        db.flush()
        base = Path(tempfile.mkdtemp(prefix="voip-route-")) / uuid4().hex
        pipe = ReproductionCapturePipeline(
            root=base / "capture", storage=FilesystemObjectStorage(base / "objects")
        )
        orch = ReproductionOrchestrator(
            registry=ReproductionProfileRegistry(PROFILE_ROOT),
            platform=MockReproductionPlatform(),
            capture_pipeline=pipe,
        )
        session = orch.create_session(
            db, case_id=case.id, profile_id="AUDIO_NOISE", device_id=device.id
        )
        session_id = session.id
        db.expire_all()
        # Patch the real adapter builder out so the task runs against the mock
        # orchestrator built above (no SSH / Poseidon in unit tests).
        monkeypatch.setattr(
            rt,
            "_build_orchestrator_for",
            lambda row, connect=False, force_legacy_platform=False: (orch, None, (lambda: None)),
        )
        watch_dispatched = []

        def fake_watch(args, queue=None):
            watch_dispatched.append(queue)

        monkeypatch.setattr(
            "app.workers.reproduction_event_tasks.watch_fxs_events.apply_async",
            fake_watch,
            raising=False,
        )
        result = rt.start_reproduction.apply(args=[session_id])
        assert result.get()["state"] == ReproductionState.WATCHING.value
        assert watch_dispatched == ["reproduction-watch"]
        db.expire_all()
        fresh = db.get(ReproductionSession, session_id)
        assert fresh.state == ReproductionState.WATCHING.value


def _patch_sessionlocal(monkeypatch, eng):
    import app.db.session as dbs

    monkeypatch.setattr(dbs, "SessionLocal", lambda: Session(eng))
    from app.workers import reproduction_tasks as rt

    monkeypatch.setattr(rt, "SessionLocal", lambda: Session(eng), raising=False)
