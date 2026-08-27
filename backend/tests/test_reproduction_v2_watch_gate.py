from __future__ import annotations

import asyncio

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.db.base import Base
from app.db.models import Case, CaseDevice, ReproductionSession
from app.workers import reproduction_event_tasks as ret


def _engine():
    eng = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(eng)
    return eng


def _seed(db: Session):
    case = Case(case_no="C-V2W", summary="v2 watch gate", status="ANALYZING")
    db.add(case)
    db.flush()
    device = CaseDevice(
        case_id=case.id, ip="10.0.0.1", ssh_port=22, sn="SN-V2W", username="root"
    )
    db.add(device)
    db.flush()
    session = ReproductionSession(
        case_id=case.id,
        device_id=device.id,
        profile_key="VOIP_GENERIC_FULL_CAPTURE",
        profile_version="1.0",
        profile_checksum="chk",
        effective_profile_snapshot={},
        state="WATCHING",
    )
    db.add(session)
    db.commit()
    return session


def _patch(monkeypatch, eng):
    import app.db.session as dbs

    monkeypatch.setattr(dbs, "SessionLocal", lambda: Session(eng))
    monkeypatch.setattr(ret, "SessionLocal", lambda: Session(eng), raising=False)


def test_watch_routes_to_v2_authority_in_v2_real_mode(monkeypatch):
    """Under V2 the watcher must validate the V2 production gate and run the
    V2-native _watch_real_v11 (it adopts the Capture V2 platform, never a second
    V1 ring) instead of being fail-closed by the V1-only gate."""
    monkeypatch.setattr(settings, "reproduction_platform_mode", "real")
    monkeypatch.setattr(settings, "capture_engine_version", "V2")
    eng = _engine()
    with Session(eng) as db:
        _patch(monkeypatch, eng)
        session = _seed(db)
        calls = {"v2": 0, "v1": 0, "watch": 0}

        def fake_v2():
            calls["v2"] += 1

        def fake_v1():
            calls["v1"] += 1

        async def fake_watch(db, session, device, max_seconds):
            calls["watch"] += 1
            return {"status": "DONE", "session_id": session.id}

        monkeypatch.setattr(ret, "assert_selected_v2_live_capture_allowed", fake_v2)
        monkeypatch.setattr(ret, "assert_v1_live_capture_allowed", fake_v1)
        monkeypatch.setattr(ret, "_watch_real_v11", fake_watch)

        result = asyncio.run(ret._watch(session.id, max_seconds=5))
        assert calls["v2"] == 1
        assert calls["v1"] == 0
        assert calls["watch"] == 1
        assert result["status"] == "DONE"


def test_watch_routes_to_v1_authority_in_v1_real_mode(monkeypatch):
    """V1 mode keeps the legacy fail-closed V1 authority gate."""
    monkeypatch.setattr(settings, "reproduction_platform_mode", "real")
    monkeypatch.setattr(settings, "capture_engine_version", "V1")
    eng = _engine()
    with Session(eng) as db:
        _patch(monkeypatch, eng)
        session = _seed(db)
        calls = {"v2": 0, "v1": 0, "watch": 0}

        def fake_v2():
            calls["v2"] += 1

        def fake_v1():
            calls["v1"] += 1

        async def fake_watch(db, session, device, max_seconds):
            calls["watch"] += 1
            return {"status": "DONE", "session_id": session.id}

        monkeypatch.setattr(ret, "assert_selected_v2_live_capture_allowed", fake_v2)
        monkeypatch.setattr(ret, "assert_v1_live_capture_allowed", fake_v1)
        monkeypatch.setattr(ret, "_watch_real_v11", fake_watch)

        result = asyncio.run(ret._watch(session.id, max_seconds=5))
        assert calls["v1"] == 1
        assert calls["v2"] == 0
        assert calls["watch"] == 1
        assert result["status"] == "DONE"
