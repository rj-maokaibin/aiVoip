"""Regression tests for the SCP exact-download transport.

Covers:
- ExactScpDownloader forwards to adapter.scp_get with exact path semantics
- adapter.scp_get one-call contract (no retry here)
- GateSftpAdapter.scp_get fallback for adapter without native scp_get
- build_capture_v2_c transport selection (scp vs sftp) and invalid transport
- FaultInjectingAdapter.scp_get deterministic before/after failpoints
"""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.capture_v2.errors import CaptureV2Error
from app.capture_v2.factory import build_capture_v2_c
from app.capture_v2.gate.faults import FaultInjectingAdapter, GateFaultPlan
from app.capture_v2.transfer.scp import ExactScpDownloader


class _Result:
    exit_status = 0
    stdout = "ok"
    stderr = ""


class _ScpAdapter:
    """Adapter with a native scp_get; records calls and simulates a payload."""

    def __init__(self, *, fail: Exception | None = None, payload: bytes = b"SCP-PAYLOAD"):
        self.calls = []
        self.fail = fail
        self.payload = payload

    async def scp_get(self, remote_path: str, local_path: str, timeout: float | None = None):
        self.calls.append((remote_path, local_path, timeout))
        if self.fail is not None:
            raise self.fail
        Path(local_path).write_bytes(self.payload)


def test_exact_scp_downloader_uses_scp_get_and_removes_partial(tmp_path):
    adapter = _ScpAdapter(payload=b"x" * 24)
    dl = ExactScpDownloader(adapter)
    target = tmp_path / "seg.pcap"
    target.write_bytes(b"STALE")
    asyncio.run(dl.get(remote_path="/tmp/aivoip_capture/seg.pcap", local_path=target))
    assert adapter.calls[0][0] == "/tmp/aivoip_capture/seg.pcap"
    assert target.read_bytes() == b"x" * 24  # stale file replaced exactly


def test_exact_scp_downloader_wraps_failure_as_scp_get_failed(tmp_path):
    adapter = _ScpAdapter(fail=RuntimeError("boom"))
    dl = ExactScpDownloader(adapter)
    with pytest.raises(CaptureV2Error) as exc:
        asyncio.run(dl.get(remote_path="/r/seg.pcap", local_path=tmp_path / "seg.pcap"))
    assert exc.value.code == "SCP_GET_FAILED"


def test_exact_scp_downloader_missing_adapter_method_is_adapter_not_installed(tmp_path):
    class NoScp:
        pass
    dl = ExactScpDownloader(NoScp())
    with pytest.raises(CaptureV2Error) as exc:
        asyncio.run(dl.get(remote_path="/r/seg.pcap", local_path=tmp_path / "seg.pcap"))
    assert exc.value.code == "SCP_ADAPTER_NOT_INSTALLED"


def test_gate_sftp_adapter_scp_get_falls_back_to_asyncssh_scp(monkeypatch, tmp_path):
    from app.capture_v2.gate import context

    class _Conn:
        pass

    class _BaseAdapter:
        def __init__(self):
            self.conn = _Conn()

    calls = {}

    async def _fake_scp(src, dst):
        calls["src"] = src
        calls["dst"] = dst
        Path(dst).write_bytes(b"GOT")

    import asyncssh
    monkeypatch.setattr(asyncssh, "scp", _fake_scp)

    proxy = context.GateSftpAdapter(_BaseAdapter())
    # native scp_get missing on _BaseAdapter -> fallback to asyncssh.scp
    asyncio.run(proxy.scp_get("/remote/seg.pcap", str(tmp_path / "seg.pcap"), timeout=15))
    assert calls["src"][0] is _BaseAdapter().conn or isinstance(calls["src"][0], _Conn)
    assert calls["src"][1] == "/remote/seg.pcap"
    assert (tmp_path / "seg.pcap").read_bytes() == b"GOT"


def test_gate_sftp_adapter_scp_get_uses_native_method(monkeypatch, tmp_path):
    from app.capture_v2.gate import context

    class _Native:
        def __init__(self):
            self.calls = []

        async def scp_get(self, remote_path, local_path, timeout=None):
            self.calls.append((remote_path, local_path, timeout))
            Path(local_path).write_bytes(b"NATIVE")

    native = _Native()
    proxy = context.GateSftpAdapter(native)
    asyncio.run(proxy.scp_get("/remote/seg.pcap", str(tmp_path / "seg.pcap")))
    assert native.calls[0][0] == "/remote/seg.pcap"
    assert (tmp_path / "seg.pcap").read_bytes() == b"NATIVE"


def test_fault_injecting_adapter_scp_before_get_failpoint(tmp_path):
    from app.capture_v2.gate.faults import FaultInjectingAdapter, GateFaultPlan

    plan = GateFaultPlan(sftp_fail_before_get_count=1)
    adapter = FaultInjectingAdapter(_ScpAdapter(), plan)
    target = tmp_path / "seg-before.pcap"
    with pytest.raises(CaptureV2Error) as exc:
        asyncio.run(adapter.scp_get("/r/seg.pcap", str(target)))
    assert exc.value.code == "GATE_INJECTED_SCP_FAILURE"
    assert not target.exists()


def test_fault_injecting_adapter_scp_after_get_failpoint(tmp_path):
    plan = GateFaultPlan(sftp_fail_after_get_count=1)
    adapter = FaultInjectingAdapter(_ScpAdapter(), plan)
    target = tmp_path / "seg-after.pcap"
    with pytest.raises(CaptureV2Error) as exc:
        asyncio.run(adapter.scp_get("/r/seg.pcap", str(target)))
    assert exc.value.code == "GATE_INJECTED_SCP_FAILURE"
    # underlying transfer happened (payload written) before the injected failure
    assert target.read_bytes() == b"SCP-PAYLOAD"


def test_build_capture_v2_c_transport_scp_selects_scp_downloader(monkeypatch):
    from app.capture_v2.profiles.resolver import EffectiveProfileResolver
    from app.capture_v2.profiles.schema import EffectiveCaptureProfile

    profile = EffectiveCaptureProfile(
        capture_profile_id="voip-standard", capture_profile_version="2.1.1",
        platform_profile_id="mt7621", platform_profile_version="1",
        resolved={}, checksum_sha256="0" * 64,
    )

    class _Reader:
        async def run(self, *a, **k): return ""

    monkeypatch.setattr("app.capture_v2.factory.ReadOnlyDeviceTransport", lambda adapter, **k: _Reader())
    # Use a stub adapter; transport selection should not require a live device.
    components = build_capture_v2_c(adapter=SimpleNamespace(), effective_profile=profile, transport="scp")
    from app.capture_v2.transfer.scp import ExactScpDownloader
    from app.capture_v2.transfer.sftp import ExactSftpDownloader
    assert isinstance(components["downloader"], ExactScpDownloader)
    assert not isinstance(components["downloader"], ExactSftpDownloader)

    components_sftp = build_capture_v2_c(adapter=SimpleNamespace(), effective_profile=profile, transport="sftp")
    assert isinstance(components_sftp["downloader"], ExactSftpDownloader)


def test_build_capture_v2_c_rejects_invalid_transport(monkeypatch):
    from app.capture_v2.profiles.schema import EffectiveCaptureProfile

    profile = EffectiveCaptureProfile(
        capture_profile_id="voip-standard", capture_profile_version="2.1.1",
        platform_profile_id="mt7621", platform_profile_version="1",
        resolved={}, checksum_sha256="0" * 64,
    )

    class _Reader:
        async def run(self, *a, **k): return ""

    monkeypatch.setattr("app.capture_v2.factory.ReadOnlyDeviceTransport", lambda adapter, **k: _Reader())
    with pytest.raises(CaptureV2Error) as exc:
        build_capture_v2_c(adapter=SimpleNamespace(), effective_profile=profile, transport="rsync")
    assert exc.value.code == "CAPTURE_V2_TRANSPORT_INVALID"


def test_factory_store_is_rooted_at_object_root_no_double_prefix(monkeypatch, tmp_path):
    """The durable store root must be object_root (storage_key already carries the
    capture-v2/<device>/<epoch>/... namespace). A double prefix
    (capture-v2/capture-v2/...) made the evidence collector (object_root /
    storage_key) unable to resolve durable server copies, failing
    acked_segments_have_server_copy on the real device."""
    from app.capture_v2.profiles.schema import EffectiveCaptureProfile
    from app.capture_v2.segment.models import RemoteSegmentIdentity
    from app.capture_v2.storage.local import LocalDurableSegmentStore
    from app.capture_v2.transfer.persister import SegmentPersister
    from app.core.config import settings

    profile = EffectiveCaptureProfile(
        capture_profile_id="voip-standard", capture_profile_version="2.1.1",
        platform_profile_id="mt7621", platform_profile_version="1",
        resolved={}, checksum_sha256="0" * 64,
    )

    class _Reader:
        async def run(self, *a, **k): return ""

    monkeypatch.setattr("app.capture_v2.factory.ReadOnlyDeviceTransport", lambda adapter, **k: _Reader())
    monkeypatch.setattr(settings, "reproduction_object_root", tmp_path / "objects")

    components = build_capture_v2_c(adapter=SimpleNamespace(), effective_profile=profile, transport="scp")
    store = components["store"]
    assert isinstance(store, LocalDurableSegmentStore)
    # Store root must be exactly object_root; storage_key already has capture-v2/.
    assert store.root == tmp_path / "objects"

    # Persist one object and verify the collector-style resolution finds it.
    from app.capture_v2.db_models import CaptureEpoch, CaptureEvent, CaptureSession, CaptureSegment
    from app.capture_v2.enums import CaptureEpochState, CaptureHealth, CaptureSessionState
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    engine = create_engine("sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    from app.db.base import Base
    from app.db import models as _existing_models  # noqa
    Base.metadata.create_all(engine, tables=[CaptureSession.__table__, CaptureEpoch.__table__, CaptureEvent.__table__, CaptureSegment.__table__])
    F = sessionmaker(bind=engine, expire_on_commit=False)
    with F() as db, db.begin():
        db.add(CaptureSession(id="S", reproduction_session_id="R", device_id="D1",
                              state=CaptureSessionState.PREPARING.value, health_status=CaptureHealth.HEALTHY.value,
                              capture_profile_id="p", capture_profile_version="1",
                              platform_profile_id="mt7621", platform_profile_version="1", effective_profile={}))
        db.add(CaptureEpoch(id="E", capture_session_id="S", device_id="D1", epoch_index=1, epoch_token="CAP_E",
                            lease_epoch_started=1, state=CaptureEpochState.RUNNING.value,
                            producer_pid=10, producer_starttime=20, interface="br-lan_400"))
        db.add(CaptureSegment(id="SEG", capture_session_id="S", capture_epoch_id="E", device_id="D1",
                              segment_seq=1, remote_path="/tmp/seg.pcap", remote_inode=123,
                              remote_size=24, state="DOWNLOADED"))

    persister = SegmentPersister(F, store)
    src = tmp_path / "part.pcap"
    src.write_bytes(b"\xd4\xc3\xb2\xa1" + b"\x00" * 20)  # 24-byte classic pcap
    key = persister.persist("SEG", src)

    with F() as db:
        row = db.get(CaptureSegment, "SEG")
        assert row.state == "PERSISTED"
        assert row.storage_key == key
        # Evidence collector resolves object_root / storage_key
        resolved = tmp_path / "objects" / row.storage_key
        assert resolved.is_file(), f"collector must resolve durable copy at {resolved}"
