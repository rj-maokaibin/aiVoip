from __future__ import annotations

import pytest

from app.automation.adapters.entries.web import (
    WebEntryError,
    project_voip_user_info_write_payload,
)


def _row() -> dict:
    return {
        "hdl": "0",
        "active": "1",
        "timeout": "3600",
        "disName": "7900",
        "number": "7900",
        "authId": "7102",
        "encType": 1,
        "passwd": "710211",
    }


def _expected() -> dict:
    return {
        "data": [{
            "hdl": "0",
            "active": "1",
            "timeout": "3600",
            "disName": "7900",
            "number": "7900",
            "authId": "7102",
            "passwd": "710211",
        }]
    }


def test_voip_user_info_write_projection_matches_successful_manual_save_shape() -> None:
    raw_readback = {
        "data": [_row()],
        "func": "voipUserInfo",
        "version": "1.0.0",
        "configTime": "1788848794",
        "currentTime": "1788848794",
        "configId": "1788848794",
    }

    writable = project_voip_user_info_write_payload(raw_readback)

    assert writable == _expected()
    assert "encType" not in writable["data"][0]
    assert "func" not in writable
    assert "version" not in writable
    assert "configTime" not in writable
    assert "currentTime" not in writable
    assert "configId" not in writable


def test_voip_user_info_write_projection_accepts_runtime_unwrapped_row_list() -> None:
    writable = project_voip_user_info_write_payload([_row()])

    assert writable == _expected()
    assert "encType" not in writable["data"][0]


def test_voip_user_info_write_projection_fails_closed_on_missing_required_field() -> None:
    row = _row()
    row.pop("passwd")
    with pytest.raises(WebEntryError, match="WEB_VOIP_USER_INFO_WRITE_FIELD_MISSING:passwd"):
        project_voip_user_info_write_payload({"data": [row]})
