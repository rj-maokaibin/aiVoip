from types import SimpleNamespace

import pytest

from app.capture_v2.gate import context
from app.capture_v2.gate.cli import _resolve_reproduction_session_id


def test_password_source_plain_env(monkeypatch):
    monkeypatch.setenv("CAPTURE_GATE_TEST_PASSWORD", "secret-value")
    assert context.password_from_source("CAPTURE_GATE_TEST_PASSWORD") == "secret-value"


def test_password_source_explicit_env(monkeypatch):
    monkeypatch.setenv("CAPTURE_GATE_TEST_PASSWORD", "secret-value")
    assert context.password_from_source("ENV:CAPTURE_GATE_TEST_PASSWORD") == "secret-value"


def test_password_source_db_does_not_require_secret_in_action(monkeypatch):
    class FakeDB:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def scalar(self, _statement):
            return SimpleNamespace(password="db-secret")

    monkeypatch.setattr(context, "SessionLocal", lambda: FakeDB())
    assert context.password_from_source("DB:G1TEST") == "db-secret"


def test_reproduction_session_from_state():
    assert _resolve_reproduction_session_id(
        "FROM_STATE",
        device_id="device-1",
        before_state={"reproduction_session_id": "session-1"},
    ) == "session-1"


def test_reproduction_session_from_state_is_fail_closed():
    with pytest.raises(Exception) as exc:
        _resolve_reproduction_session_id("FROM_STATE", device_id="device-1", before_state={})
    assert "R2_REPRO_SESSION_MISSING_FROM_STATE" in str(exc.value)
