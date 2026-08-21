import hashlib
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import models as _models  # noqa: F401
from app.db.base import Base
from app.capture_v2.db_models import CaptureEpoch, CaptureSegment, CaptureSession
from app.capture_v2.gate.evidence import GateEvidenceCollector


def factory():
    engine = create_engine("sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[CaptureSession.__table__, CaptureEpoch.__table__, CaptureSegment.__table__])
    F = sessionmaker(bind=engine, expire_on_commit=False)
    with F() as db, db.begin():
        db.add(CaptureSession(
            id="S", reproduction_session_id="R", device_id="D", state="WATCHING", health_status="HEALTHY",
            capture_profile_id="p", capture_profile_version="1", platform_profile_id="mt7621",
            platform_profile_version="1", effective_profile={},
        ))
        db.add(CaptureEpoch(
            id="E", capture_session_id="S", device_id="D", epoch_index=1, epoch_token="CAP",
            lease_epoch_started=1, state="RUNNING",
        ))
        db.add(CaptureSegment(
            id="SEG", capture_session_id="S", capture_epoch_id="E", device_id="D", segment_seq=1,
            remote_path="/tmp/seg", remote_inode=1, remote_size=4, state="ACKED",
            storage_key="capture-v2/D/E/seg_000000000001.pcap", server_size=4,
            sha256=hashlib.sha256(b"pcap").hexdigest(),
        ))
    return F


def test_evidence_collector_verifies_local_server_object(tmp_path):
    F = factory()
    root = tmp_path / "objects"
    target = root / "capture-v2/D/E/seg_000000000001.pcap"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"pcap")
    collector = GateEvidenceCollector(session_factory=F, object_root=root)
    class P: server_dir = tmp_path / "server"
    result = collector.collect_server_store(paths=P(), capture_session_id="S")
    obj = result["objects"][0]
    assert obj["exists"] is True
    assert obj["actual_size"] == 4
    assert obj["actual_sha256"] == hashlib.sha256(b"pcap").hexdigest()
