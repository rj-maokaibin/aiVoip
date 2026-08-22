from __future__ import annotations

import hashlib
import io
import zipfile
from types import SimpleNamespace

from app.api.v1 import evidence_reports as api
from app.services import evidence_report_artifacts as svc


class _Storage:
    def __init__(self, objects=None):
        self.objects = dict(objects or {})

    def get_bytes(self, key):
        return self.objects[key]

    def presigned_get(self, key, _ttl):
        return f"https://object.invalid/{key}?signed=1"


class _BundleDB:
    def __init__(self, report, evidences):
        self.report = report
        self.evidences = list(evidences)
        self.flushed = 0

    def get(self, _model, key):
        return self.report if key == self.report.id else None

    def scalars(self, _stmt):
        return iter(self.evidences)

    def flush(self):
        self.flushed += 1


class _DownloadDB:
    def __init__(self, report):
        self.report = report
        self.commits = 0

    def get(self, _model, key):
        return self.report if key == self.report.id else None

    def commit(self):
        self.commits += 1


def _artifact(*, aid, atype, filename, key):
    return SimpleNamespace(
        id=aid,
        type=atype,
        filename=filename,
        object_key=key,
        content_type="application/octet-stream",
    )


def _evidence(*, eid, etype, filename, key):
    return SimpleNamespace(
        id=eid,
        type=etype,
        filename=filename,
        object_key=key,
        created_at=None,
    )


def _report():
    return SimpleNamespace(
        id="report-1",
        version=3,
        case_id="case-1",
        session_id="session-1",
        call_id="call-1",
        scope_type="CALL",
        scope_id="call-1",
        bundle_object_key=None,
    )


def _read_sums(zf: zipfile.ZipFile) -> dict[str, str]:
    rows = {}
    for line in zf.read("SHA256SUMS").decode("utf-8").splitlines():
        digest, path = line.split("  ", 1)
        rows[path] = digest
    return rows


def test_fr027_internal_bundle_sha256sums_match_every_payload(monkeypatch):
    report = _report()
    artifacts = [
        _artifact(aid="art-json", atype="PRELIMINARY_REPORT_JSON", filename="report.json", key="a/report"),
        _artifact(aid="art-img", atype="WAVEFORM_PNG", filename="wave.png", key="a/wave"),
        _artifact(aid="art-wav", atype="PCM_WAV", filename="pcm.wav", key="a/pcm"),
    ]
    evidences = [
        _evidence(eid="ev-pcap", etype="PCAP", filename="capture.pcap", key="e/pcap"),
        _evidence(eid="ev-log", etype="DEBUG_LOG", filename="debug.log", key="e/log"),
    ]
    storage = _Storage({
        "a/report": b'{"status":"ok"}',
        "a/wave": b"PNG-BYTES",
        "a/pcm": b"RIFF-WAV-BYTES",
        "e/pcap": b"PCAP-BYTES",
        "e/log": b"DEBUG-BYTES",
    })
    db = _BundleDB(report, evidences)
    captured = {}
    audits = []

    monkeypatch.setattr(svc, "report_artifacts", lambda _db, _rid: artifacts)

    def fake_persist(_db, _storage, **kwargs):
        captured["zip"] = kwargs["data"]
        return SimpleNamespace(id="bundle-1", object_key="bundle/object.zip", case_id=report.case_id)

    monkeypatch.setattr(svc, "persist_artifact", fake_persist)
    monkeypatch.setattr(svc, "audit", lambda *args, **kwargs: audits.append(kwargs))

    row = svc.build_evidence_bundle(db, report_id=report.id, profile="INTERNAL_FULL", actor="tester", storage=storage)
    assert row.object_key == "bundle/object.zip"
    assert report.bundle_object_key == "bundle/object.zip"
    assert audits and audits[-1]["event_type"] == "EVIDENCE_BUNDLE_GENERATED"

    with zipfile.ZipFile(io.BytesIO(captured["zip"]), "r") as zf:
        names = set(zf.namelist())
        assert "manifest.json" in names
        assert "SHA256SUMS" in names
        assert any(name.startswith("report/") and name.endswith("report.json") for name in names)
        assert any(name.startswith("images/") and name.endswith("wave.png") for name in names)
        assert any(name.startswith("audio/full/") and name.endswith("pcm.wav") for name in names)
        assert any(name.startswith("pcap/") and name.endswith("capture.pcap") for name in names)
        assert any(name.startswith("debug/") and name.endswith("debug.log") for name in names)
        sums = _read_sums(zf)
        assert set(sums) == names - {"SHA256SUMS"}
        for path, expected in sums.items():
            assert hashlib.sha256(zf.read(path)).hexdigest() == expected


def test_fr027_share_safe_excludes_raw_evidence_and_full_wav(monkeypatch):
    report = _report()
    artifacts = [
        _artifact(aid="art-img", atype="WAVEFORM_PNG", filename="wave.png", key="a/wave"),
        _artifact(aid="art-clip", atype="AUDIO_CLIP", filename="clip.wav", key="a/clip"),
        _artifact(aid="art-wav", atype="PCM_WAV", filename="full.wav", key="a/full"),
    ]
    evidences = [_evidence(eid="ev-pcap", etype="PCAP", filename="capture.pcap", key="e/pcap")]
    storage = _Storage({"a/wave": b"PNG", "a/clip": b"CLIP", "a/full": b"FULL", "e/pcap": b"PCAP"})
    db = _BundleDB(report, evidences)
    captured = {}
    monkeypatch.setattr(svc, "report_artifacts", lambda _db, _rid: artifacts)

    def fake_persist(_db, _storage, **kwargs):
        captured["zip"] = kwargs["data"]
        return SimpleNamespace(id="bundle-share", object_key="bundle/share.zip", case_id=report.case_id)

    monkeypatch.setattr(svc, "persist_artifact", fake_persist)
    monkeypatch.setattr(svc, "audit", lambda *args, **kwargs: None)

    svc.build_evidence_bundle(db, report_id=report.id, profile="SHARE_SAFE", storage=storage)
    with zipfile.ZipFile(io.BytesIO(captured["zip"]), "r") as zf:
        names = set(zf.namelist())
        assert any(name.startswith("images/") for name in names)
        assert any(name.startswith("audio/clips/") for name in names)
        assert not any(name.startswith("audio/full/") for name in names)
        assert not any(name.startswith("pcap/") for name in names)
        assert not any(name.startswith("debug/") for name in names)


def test_fr027_actual_download_is_permission_boundary_audited_then_redirected(monkeypatch):
    report = _report()
    report.bundle_object_key = "bundle/object.zip"
    db = _DownloadDB(report)
    storage = _Storage()
    events = []

    monkeypatch.setattr(api, "_enabled", lambda: None)
    monkeypatch.setattr(api, "ObjectStorage", lambda: storage)
    monkeypatch.setattr(api, "audit", lambda *args, **kwargs: events.append(kwargs))

    response = api.download_bundle(report.id, db=db, identity=SimpleNamespace(actor_id="reader"))
    assert response.status_code == 307
    assert response.headers["location"].startswith("https://object.invalid/bundle/object.zip")
    assert db.commits == 1
    assert events and events[-1]["event_type"] == "EVIDENCE_BUNDLE_DOWNLOADED"
    assert events[-1]["target_id"] == report.id
