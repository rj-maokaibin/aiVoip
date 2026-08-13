from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(slots=True)
class PcmTap:
    name: str
    direction: str
    dst_port: int


@dataclass(slots=True)
class PcmProfile:
    id: str
    sample_rate: int | None
    bit_depth: int | None
    signed: bool | None
    endian: str | None
    channels: int | None
    packet_payload_bytes: int | None
    expected_packet_interval_ms: float | None
    session_gap_ms: float
    taps: list[PcmTap]
    schema_version: int = 1
    version: str = "1.0.0"
    format_status: str = "VERIFIED"
    header_length: int = 0
    payload_offset: int = 0
    pcm_payload_bytes: int | None = None
    checksum: str | None = None
    source_path: str | None = None

    @property
    def can_decode(self) -> bool:
        return (
            self.format_status == "VERIFIED"
            and self.sample_rate is not None
            and self.bit_depth is not None
            and self.signed is not None
            and self.endian is not None
            and self.channels is not None
        )

    @property
    def dtype(self) -> str:
        if not self.can_decode:
            raise ValueError("PCM_FORMAT_UNAVAILABLE")
        if self.bit_depth != 16 or not self.signed:
            raise ValueError("PCM_FORMAT_NOT_IMPLEMENTED")
        return "<i2" if str(self.endian).lower() == "little" else ">i2"

    @property
    def decoded_payload_bytes(self) -> int | None:
        if self.pcm_payload_bytes is not None:
            return self.pcm_payload_bytes
        if self.packet_payload_bytes is None:
            return None
        return max(0, int(self.packet_payload_bytes) - int(self.payload_offset))

    def metadata(self) -> dict:
        return {
            "profile_id": self.id,
            "profile_version": self.version,
            "format_status": self.format_status,
            "profile_checksum": self.checksum,
        }

    def snapshot(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "version": self.version,
            "format_status": self.format_status,
            "checksum": self.checksum,
            "transport": {
                "udp_payload_bytes": self.packet_payload_bytes,
                "header_length": self.header_length,
                "payload_offset": self.payload_offset,
                "pcm_payload_bytes": self.decoded_payload_bytes,
                "expected_packet_interval_ms": self.expected_packet_interval_ms,
                "session_gap_ms": self.session_gap_ms,
            },
            "format": {
                "sample_rate": self.sample_rate,
                "bit_depth": self.bit_depth,
                "signed": self.signed,
                "endian": self.endian,
                "channels": self.channels,
            },
            "taps": [
                {"name": t.name, "direction": t.direction, "dst_port": t.dst_port}
                for t in self.taps
            ],
        }

    @classmethod
    def raw(cls, *, id: str, taps: list[PcmTap], packet_payload_bytes: int | None = None, session_gap_ms: float = 100.0) -> "PcmProfile":
        return cls(
            id=id,
            sample_rate=None,
            bit_depth=None,
            signed=None,
            endian=None,
            channels=None,
            packet_payload_bytes=packet_payload_bytes,
            expected_packet_interval_ms=None,
            session_gap_ms=session_gap_ms,
            taps=taps,
            format_status="RAW",
            version="0.0.0-raw",
        )


def _checksum(raw: dict) -> str:
    data=json.dumps(raw,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def load_pcm_profile(path: str | Path) -> PcmProfile:
    source=Path(path)
    raw=yaml.safe_load(source.read_text(encoding="utf-8"))
    if not isinstance(raw,dict):
        raise ValueError("PCM_PROFILE_DOCUMENT_INVALID")
    if int(raw.get("schema_version",1)) != 1:
        raise ValueError("PCM_PROFILE_SCHEMA_UNSUPPORTED")
    status=str(raw.get("format_status","VERIFIED")).upper()
    if status not in {"VERIFIED","RAW"}:
        raise ValueError("PCM_PROFILE_FORMAT_STATUS_INVALID")
    transport=raw.get("transport") or {}
    fmt=raw.get("format") or {}
    if status == "VERIFIED":
        required=("sample_rate","bit_depth","signed","endian","channels")
        missing=[key for key in required if key not in fmt]
        if missing:
            raise ValueError(f"PCM_PROFILE_FORMAT_FIELDS_MISSING:{','.join(missing)}")
    payload_bytes=transport.get("payload_bytes")
    payload_offset=int(transport.get("payload_offset",0))
    pcm_payload_bytes=transport.get("pcm_payload_bytes")
    profile=PcmProfile(
        id=str(raw["id"]),
        sample_rate=int(fmt["sample_rate"]) if fmt.get("sample_rate") is not None else None,
        bit_depth=int(fmt["bit_depth"]) if fmt.get("bit_depth") is not None else None,
        signed=bool(fmt["signed"]) if fmt.get("signed") is not None else None,
        endian=str(fmt["endian"]) if fmt.get("endian") is not None else None,
        channels=int(fmt["channels"]) if fmt.get("channels") is not None else None,
        packet_payload_bytes=int(payload_bytes) if payload_bytes is not None else None,
        expected_packet_interval_ms=float(transport["expected_interval_ms"]) if transport.get("expected_interval_ms") is not None else None,
        session_gap_ms=float(transport.get("session_gap_ms",100)),
        taps=[PcmTap(name=t["name"],direction=t["direction"],dst_port=int(t["dst_port"])) for t in raw.get("taps",[])],
        schema_version=int(raw.get("schema_version",1)),
        version=str(raw.get("version","1.0.0")),
        format_status=status,
        header_length=int(transport.get("header_length",payload_offset)),
        payload_offset=payload_offset,
        pcm_payload_bytes=int(pcm_payload_bytes) if pcm_payload_bytes is not None else None,
        checksum=_checksum(raw),
        source_path=str(source),
    )
    if profile.packet_payload_bytes is not None and profile.payload_offset > profile.packet_payload_bytes:
        raise ValueError("PCM_PROFILE_PAYLOAD_OFFSET_INVALID")
    return profile
