import json

import pytest

from app.capture_v2.errors import CaptureV2Error
from app.capture_v2.runtime import (
    assert_selected_v2_live_capture_allowed,
    assert_v1_live_capture_allowed,
)
from app.core.config import settings


def _artifact(path, *, rollback=False):
    path.write_text(json.dumps({
        "schema_version": "capture-v2-release-gate-v1",
        "software_gate_passed": True,
        "real_ownership_gate_passed": True,
        "real_segment_gate_passed": True,
        "readiness_gate_passed": True,
        "coverage_gate_passed": True,
        "e2e_gate_passed": True,
        "rollback_gate_passed": rollback,
        "approved": True,
    }))
    return path


def _select_v2_rehearsal(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "capture_engine_version", "V2")
    monkeypatch.setattr(settings, "capture_v2_production_enabled", False)
    monkeypatch.setattr(
        settings, "capture_v2_release_gate_artifact", _artifact(tmp_path / "gate.json")
    )
    monkeypatch.setenv("CAPTURE_V2_ACTIVATION_REHEARSAL", "true")


def test_v2_rehearsal_is_disabled_by_default(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "capture_engine_version", "V2")
    monkeypatch.setattr(settings, "capture_v2_production_enabled", False)
    monkeypatch.setattr(
        settings, "capture_v2_release_gate_artifact", _artifact(tmp_path / "gate.json")
    )
    monkeypatch.delenv("CAPTURE_V2_ACTIVATION_REHEARSAL", raising=False)
    with pytest.raises(CaptureV2Error) as exc:
        assert_selected_v2_live_capture_allowed()
    assert exc.value.code == "CAPTURE_V2_ACTIVATION_REHEARSAL_DISABLED"


def test_v2_rehearsal_allows_only_pre_rollback_all_green(monkeypatch, tmp_path):
    _select_v2_rehearsal(monkeypatch, tmp_path)
    selected = assert_selected_v2_live_capture_allowed()
    assert selected["mode"] == "ACTIVATION_REHEARSAL"
    assert selected["artifact"]["rollback_gate_passed"] is False


def test_legacy_semantics_reuse_remains_explicit_and_gate_backed(monkeypatch, tmp_path):
    _select_v2_rehearsal(monkeypatch, tmp_path)
    monkeypatch.delenv("CAPTURE_V2_REUSE_LEGACY_REPRODUCTION_SEMANTICS", raising=False)
    with pytest.raises(CaptureV2Error) as exc:
        assert_v1_live_capture_allowed()
    assert exc.value.code == "CAPTURE_V2_SELECTED_V1_AUTHORITY_FORBIDDEN"

    monkeypatch.setenv("CAPTURE_V2_REUSE_LEGACY_REPRODUCTION_SEMANTICS", "true")
    assert_v1_live_capture_allowed() is None


def test_rehearsal_rejected_once_rollback_is_already_proven(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "capture_engine_version", "V2")
    monkeypatch.setattr(settings, "capture_v2_production_enabled", False)
    monkeypatch.setattr(
        settings,
        "capture_v2_release_gate_artifact",
        _artifact(tmp_path / "gate.json", rollback=True),
    )
    monkeypatch.setenv("CAPTURE_V2_ACTIVATION_REHEARSAL", "true")
    with pytest.raises(CaptureV2Error) as exc:
        assert_selected_v2_live_capture_allowed()
    assert exc.value.code == "CAPTURE_V2_REHEARSAL_NO_LONGER_REQUIRED"


def test_platform_factory_routes_v2_watcher_but_can_force_semantic_only_legacy(
    monkeypatch, tmp_path
):
    _select_v2_rehearsal(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "reproduction_platform_mode", "real")
    monkeypatch.setenv("CAPTURE_V2_REUSE_LEGACY_REPRODUCTION_SEMANTICS", "true")

    from app.reproduction.platform_factory import build_orchestrator

    class FakeAdapter:
        ip = "127.0.0.1"
        port = 22
        username = "root"
        password = "secret"
        aim_prompt = "AIM>"
        aim_executable = "aim"
        kex_algs = ["diffie-hellman-group14-sha1"]

    orch, close = build_orchestrator(adapter=FakeAdapter(), connect=False)
    try:
        assert orch.platform.uses_capture_v2 is True
        assert orch.platform.platform_id == "ruijie-voip-capture-v2"
    finally:
        close()

    legacy, legacy_close = build_orchestrator(
        adapter=FakeAdapter(), connect=False, force_legacy_platform=True
    )
    try:
        assert getattr(legacy.platform, "uses_capture_v2", False) is False
        assert legacy.platform.platform_id == "ruijie-voip-aim-real"
    finally:
        legacy_close()
