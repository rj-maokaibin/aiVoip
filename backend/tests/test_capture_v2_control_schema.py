from datetime import datetime, timedelta, timezone

import pytest

from app.capture_v2.control.schema import ControlActionType, RemoteAction


def _raw(**overrides):
    now = datetime.now(timezone.utc)
    raw = {
        "schema_version": "capture-v2-remote-action-v1",
        "action_id": "R3-08-001",
        "sequence": 1,
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
        "action_type": "GATE_EVALUATE",
        "parameters": {"bundle": "/tmp/bundle", "gate_id": "R3-08"},
        "safety": {},
    }
    raw.update(overrides)
    return raw


def test_action_schema_parses_and_hash_is_stable():
    a = RemoteAction.from_dict(_raw())
    b = RemoteAction.from_dict(_raw(created_at=a.created_at.isoformat(), expires_at=a.expires_at.isoformat()))
    assert a.action_type == ControlActionType.GATE_EVALUATE
    assert a.digest() == b.digest()


def test_action_rejects_unknown_fields():
    with pytest.raises(ValueError, match="UNKNOWN_ACTION_FIELDS"):
        RemoteAction.from_dict(_raw(shell="rm -rf /"))


def test_action_rejects_naive_time_and_bad_id():
    with pytest.raises(ValueError, match="CREATED_AT_MUST_BE_TZ_AWARE"):
        RemoteAction.from_dict(_raw(created_at="2026-08-21T10:00:00"))
    with pytest.raises(ValueError, match="ACTION_ID_INVALID"):
        RemoteAction.from_dict(_raw(action_id="bad id;rm"))
