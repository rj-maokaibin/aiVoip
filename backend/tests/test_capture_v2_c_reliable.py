from __future__ import annotations

import hashlib
import struct
from pathlib import Path

import pytest

from app.capture_v2.errors import CaptureV2Error
from app.capture_v2.segment.pcap import validate_classic_pcap
from app.capture_v2.storage.local import LocalDurableSegmentStore


def _empty_pcap(path: Path):
    # little-endian classic PCAP global header, Ethernet link type
    path.write_bytes(struct.pack("<IHHIIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1))


def test_header_only_24_byte_pcap_is_valid_silent_segment(tmp_path):
    p = tmp_path / "silent.pcap"
    _empty_pcap(p)
    result = validate_classic_pcap(p)
    assert result.valid is True
    assert result.packet_count == 0
    assert result.size == 24
    assert result.first_packet_ts is None


def test_local_durable_store_is_idempotent_but_never_overwrites_conflict(tmp_path):
    source = tmp_path / "a.pcap"
    _empty_pcap(source)
    sha = hashlib.sha256(source.read_bytes()).hexdigest()
    store = LocalDurableSegmentStore(tmp_path / "store")
    first = store.persist(source_path=source, storage_key="D/E/seg.pcap", sha256=sha)
    second = store.persist(source_path=source, storage_key="D/E/seg.pcap", sha256=sha)
    assert first == second
    target = tmp_path / "store" / "D/E/seg.pcap"
    target.write_bytes(b"different durable evidence")
    with pytest.raises(CaptureV2Error) as exc:
        store.persist(source_path=source, storage_key="D/E/seg.pcap", sha256=sha)
    assert exc.value.code == "SEGMENT_INTEGRITY_CONFLICT"
