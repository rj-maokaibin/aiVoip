import pytest

from app.capture_v2.errors import CaptureV2Error
from app.capture_v2.runtime import assert_v1_live_capture_allowed, capture_v2_enabled
from app.core.config import settings


def test_v1_default_allows_legacy_live_authority(monkeypatch):
    monkeypatch.setattr(settings, "capture_engine_version", "V1")
    assert capture_v2_enabled() is False
    assert_v1_live_capture_allowed()


def test_v2_phase_ab_fails_closed_before_v1_live_authority(monkeypatch):
    monkeypatch.setattr(settings, "capture_engine_version", "V2")
    assert capture_v2_enabled() is True
    with pytest.raises(CaptureV2Error) as exc:
        assert_v1_live_capture_allowed()
    assert exc.value.code == "CAPTURE_V2_SELECTED_V1_AUTHORITY_FORBIDDEN"
