from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.capture_v2.db_models  # noqa: F401

from app.core.config import settings
from app.db.base import Base
from app.db.models import Case, CaseDevice, ReproductionSession
from app.reproduction.mock_platform import MockReproductionPlatform
from app.reproduction.orchestrator import ReproductionOrchestrator
from app.reproduction.profile import ReproductionProfileRegistry
from tools.m7_acceptance_strict_audit import (
    analyzer_uses_target_evidence,
    channel_health_detail,
    is_strict_real_session,
    linked_analyzers_with_provenance,
    select_target_session,
)

PROFILE_ROOT = Path(__file__).resolve().parents[2] / "profiles"


def _engine():
    eng = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(eng)
    return eng


def test_strict_real_session_accepts_known_ids_and_exact_legacy_resolver_only():
    assert is_strict_real_session(
        SimpleNamespace(platform_profile_id="ruijie-voip-aim-real", voice_runtime_context_json={})
    )
    assert is_strict_real_session(
        SimpleNamespace(platform_profile_id="ruijie-voip-capture-v2", voice_runtime_context_json={})
    )
    assert is_strict_real_session(
        SimpleNamespace(
            platform_profile_id="mock-voip-platform",
            voice_runtime_context_json={"resolver_id": "REAL_VOICE_CONTEXT_V1"},
        )
    )
    assert not is_strict_real_session(
        SimpleNamespace(
            platform_profile_id="mock-voip-platform",
            voice_runtime_context_json={"resolver_id": "NOT_REAL_BUT_CONTAINS_REAL"},
        )
    )
    assert not is_strict_real_session(
        SimpleNamespace(platform_profile_id="mock-voip-platform", voice_runtime_context_json={})
    )


def test_select_target_session_requires_latest_reproduction_itself_to_be_real():
    now = datetime.now(timezone.utc)
    older_real = SimpleNamespace(
        id="real-old",
        platform_profile_id="ruijie-voip-capture-v2",
        voice_runtime_context_json={},
        created_at=now,
    )
    newer_mock = SimpleNamespace(
        id="mock-new",
        platform_profile_id="mock-voip-platform",
        voice_runtime_context_json={"resolver_id": "MOCK_VOICE_CONTEXT_V1"},
        created_at=now + timedelta(minutes=5),
    )
    assert select_target_session([older_real, newer_mock]) is None

    newest_real = SimpleNamespace(
        id="real-new",
        platform_profile_id="ruijie-voip-aim-real",
        voice_runtime_context_json={},
        created_at=now + timedelta(minutes=10),
    )
    assert select_target_session([older_real, newer_mock, newest_real]).id == "real-new"


def test_analyzer_must_reference_target_session_evidence():
    linked = SimpleNamespace(input_evidence_ids=["e-target", "e-other"])
    unrelated = SimpleNamespace(input_evidence_ids=["e-other"])
    assert analyzer_uses_target_evidence(linked, {"e-target"}) is True
    assert analyzer_uses_target_evidence(unrelated, {"e-target"}) is False


def test_analyzer_provenance_closure_allows_derived_chain_but_not_unrelated_runs():
    packet = SimpleNamespace(
        id="packet",
        status="SUCCESS",
        input_evidence_ids=["raw-pcap"],
        output_evidence_ids=["packet-derived"],
    )
    media = SimpleNamespace(
        id="media",
        status="SUCCESS",
        input_evidence_ids=["packet-derived"],
        output_evidence_ids=["media-derived"],
    )
    unrelated = SimpleNamespace(
        id="unrelated",
        status="SUCCESS",
        input_evidence_ids=["other-case-evidence"],
        output_evidence_ids=["bad-derived"],
    )
    linked, closure = linked_analyzers_with_provenance(
        [media, unrelated, packet], {"raw-pcap"}
    )
    assert [row.id for row in linked] == ["packet", "media"]
    assert closure == {"raw-pcap", "packet-derived", "media-derived"}


def test_channel_health_detail_surfaces_packet_count_status_and_health_payload():
    row = SimpleNamespace(
        channel="PCM_RX",
        status="HEALTHY",
        packet_count=123,
        last_observed_at="2026-08-27T10:00:00Z",
        health_json={"source": "capture-v2"},
    )
    detail = channel_health_detail([row])
    assert detail["PCM_RX"]["status"] == "HEALTHY"
    assert detail["PCM_RX"]["packet_count"] == 123
    assert detail["PCM_RX"]["health_json"] == {"source": "capture-v2"}


def test_start_task_persists_actual_real_platform_identity(monkeypatch):
    """Regression for the historical C06 report showing mock-voip-platform."""
    from app.workers import reproduction_tasks as rt

    eng = _engine()
    monkeypatch.setattr(rt, "SessionLocal", lambda: Session(eng), raising=False)
    monkeypatch.setattr(settings, "app_env", "development")
    monkeypatch.setattr(settings, "reproduction_platform_mode", "mock")
    monkeypatch.setattr(settings, "profile_root", PROFILE_ROOT)

    with Session(eng) as db:
        case = Case(case_no="M7-AUDIT-PLATFORM", summary="platform identity", status="NEW")
        db.add(case)
        db.flush()
        device = CaseDevice(
            case_id=case.id,
            ip="198.51.100.40",
            ssh_port=22,
            sn="SN-M7-AUDIT",
            username="root",
        )
        db.add(device)
        db.flush()
        creator = ReproductionOrchestrator(
            registry=ReproductionProfileRegistry(PROFILE_ROOT),
            platform=MockReproductionPlatform(),
        )
        session = creator.create_session(
            db,
            case_id=case.id,
            profile_id="VOIP_GENERIC_FULL_CAPTURE",
            device_id=device.id,
        )
        sid = session.id
        assert session.platform_profile_id == "mock-voip-platform"
        db.commit()

    class FakeRealOrchestrator:
        platform = SimpleNamespace(platform_id="ruijie-voip-capture-v2", version="2.1.1")

        @staticmethod
        def start(db, *, session, owner_worker, actor):
            session.state = "FAILED"
            return session

    monkeypatch.setattr(
        rt,
        "_build_orchestrator_for",
        lambda row, connect=False, force_legacy_platform=False: (
            FakeRealOrchestrator(),
            None,
            lambda: None,
        ),
    )
    monkeypatch.setattr(rt, "ensure_reproduction_diagnosis", lambda session_id: {"status": "TEST"})

    result = rt.start_reproduction.apply(args=[sid]).get()
    assert result["session_id"] == sid

    with Session(eng) as db:
        fresh = db.get(ReproductionSession, sid)
        assert fresh.platform_profile_id == "ruijie-voip-capture-v2"
        assert fresh.platform_profile_version == "2.1.1"
