from __future__ import annotations

from types import SimpleNamespace

from app.integrations import storage as storage_module
from app.integrations.storage import FilesystemObjectStorage, materialize_evidence


def test_reproduction_staging_is_resolved_without_constructing_permanent_storage(
    tmp_path, monkeypatch,
):
    staging = FilesystemObjectStorage(tmp_path / "staging")
    staging.put_bytes("cases/c1/segment.pcap", b"pcap")
    monkeypatch.setattr(storage_module, "reproduction_object_storage", lambda: staging)

    class ForbiddenPermanentStorage:
        def __init__(self):
            raise AssertionError("permanent storage must be lazy")

    monkeypatch.setattr(storage_module, "ObjectStorage", ForbiddenPermanentStorage)
    evidence = SimpleNamespace(
        session_id="session-1", object_key="cases/c1/segment.pcap")
    destination = tmp_path / "input.pcap"

    backend = materialize_evidence(evidence, destination)

    assert backend == "reproduction"
    assert destination.read_bytes() == b"pcap"


def test_non_reproduction_evidence_uses_permanent_storage(tmp_path):
    permanent = FilesystemObjectStorage(tmp_path / "permanent")
    permanent.put_bytes("cases/c1/classic.pcap", b"classic")
    evidence = SimpleNamespace(session_id=None, object_key="cases/c1/classic.pcap")
    destination = tmp_path / "classic.pcap"

    backend = materialize_evidence(
        evidence, destination, permanent_storage=permanent)

    assert backend == "permanent"
    assert destination.read_bytes() == b"classic"
