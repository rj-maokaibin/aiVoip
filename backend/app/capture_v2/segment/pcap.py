from __future__ import annotations

import struct
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.capture_v2.errors import CaptureV2Error

_PCAP_MAGIC = {
    b"\xd4\xc3\xb2\xa1": ("<", False),
    b"\xa1\xb2\xc3\xd4": (">", False),
    b"\x4d\x3c\xb2\xa1": ("<", True),
    b"\xa1\xb2\x3c\x4d": (">", True),
}


@dataclass(frozen=True)
class PcapValidation:
    valid: bool
    packet_count: int
    first_packet_ts: datetime | None
    last_packet_ts: datetime | None
    size: int


def validate_classic_pcap(path: Path) -> PcapValidation:
    path = Path(path)
    size = path.stat().st_size
    if size < 24:
        raise CaptureV2Error("PCAP_INVALID_TRUNCATED_HEADER", details={"size": size})
    with path.open("rb") as fh:
        header = fh.read(24)
        fmt = _PCAP_MAGIC.get(header[:4])
        if fmt is None:
            raise CaptureV2Error("PCAP_MAGIC_UNSUPPORTED", details={"magic": header[:4].hex()})
        endian, nano = fmt
        count = 0
        first = last = None
        while True:
            ph = fh.read(16)
            if not ph:
                break
            if len(ph) != 16:
                raise CaptureV2Error("PCAP_INVALID_TRUNCATED_PACKET_HEADER")
            ts_sec, ts_frac, incl_len, _orig_len = struct.unpack(endian + "IIII", ph)
            payload = fh.read(incl_len)
            if len(payload) != incl_len:
                raise CaptureV2Error("PCAP_INVALID_TRUNCATED_PACKET", details={"incl_len": incl_len})
            scale = 1_000_000_000 if nano else 1_000_000
            ts = datetime.fromtimestamp(ts_sec + ts_frac / scale, tz=timezone.utc)
            first = first or ts
            last = ts
            count += 1
    return PcapValidation(True, count, first, last, size)
