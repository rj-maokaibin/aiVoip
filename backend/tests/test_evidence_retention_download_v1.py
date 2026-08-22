from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.v1 import evidences as api


class _DB:
    def __init__(self, row):
        self.row = row
        self.commits = 0

    def get(self, _model, key):
        return self.row if key == self.row.id else None

    def commit(self):
        self.commits += 1


def _row(*, payload_available=True):
    return SimpleNamespace(
        id="ev-1",
        object_key="case/ev-1.pcap",
        metadata_json={"payload_available": payload_available},
    )


def test_expired_raw_evidence_download_is_explicit_410_and_never_presigned(monkeypatch):
    row = _row(payload_available=False)
    db = _DB(row)
    presign_calls = []

    monkeypatch.setattr(api, "ensure_retention_state", lambda _db, _row: SimpleNamespace(status="EXPIRED"))
    monkeypatch.setattr(api, "ObjectStorage", lambda: SimpleNamespace(presigned_get=lambda key: presign_calls.append(key)))

    with pytest.raises(HTTPException) as exc:
        api.download(row.id, db=db, _identity=SimpleNamespace())

    assert exc.value.status_code == 410
    assert exc.value.detail == "EVIDENCE_PAYLOAD_EXPIRED"
    assert presign_calls == []
    assert db.commits == 1


def test_active_raw_evidence_download_still_returns_presigned_url(monkeypatch):
    row = _row(payload_available=True)
    db = _DB(row)

    monkeypatch.setattr(api, "ensure_retention_state", lambda _db, _row: SimpleNamespace(status="ACTIVE"))
    monkeypatch.setattr(api, "ObjectStorage", lambda: SimpleNamespace(presigned_get=lambda key: f"https://object.invalid/{key}"))

    result = api.download(row.id, db=db, _identity=SimpleNamespace())

    assert result == {"url": "https://object.invalid/case/ev-1.pcap"}
    assert db.commits == 1


def test_missing_evidence_remains_404():
    db = _DB(_row())
    with pytest.raises(HTTPException) as exc:
        api.download("missing", db=db, _identity=SimpleNamespace())
    assert exc.value.status_code == 404
    assert exc.value.detail == "EVIDENCE_NOT_FOUND"
