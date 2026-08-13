from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class SipData:
    method: str | None = None
    status_code: int | None = None
    reason_phrase: str | None = None
    call_id: str | None = None
    cseq: int | None = None
    cseq_method: str | None = None
    from_uri: str | None = None
    to_uri: str | None = None
    from_tag: str | None = None
    to_tag: str | None = None
    via_branch: str | None = None
    contact: str | None = None
    content_type: str | None = None
    request_uri: str | None = None
    raw_start_line: str | None = None


@dataclass(slots=True)
class SdpData:
    raw: str | None = None
    connection_address: str | None = None
    media_port: int | None = None
    media_protocol: str | None = None
    media_payload_types: list[int] = field(default_factory=list)
    attributes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RtpData:
    ssrc: int | None = None
    sequence: int | None = None
    timestamp: int | None = None
    payload_type: int | None = None
    marker: bool | None = None
    payload_size: int | None = None
    payload_hex: str | None = None


@dataclass(slots=True)
class RtcpData:
    packet_type: str | None = None
    ssrc: int | None = None
    fraction_lost: float | None = None
    cumulative_lost: int | None = None
    jitter: int | None = None
    lsr: int | None = None
    dlsr: int | None = None


@dataclass(slots=True)
class NormalizedPacket:
    frame_number: int
    timestamp: float
    src_ip: str | None = None
    dst_ip: str | None = None
    transport: str | None = None
    src_port: int | None = None
    dst_port: int | None = None
    protocols: list[str] = field(default_factory=list)
    sip: SipData | None = None
    sdp: SdpData | None = None
    rtp: RtpData | None = None
    rtcp: RtcpData | None = None
    raw_fields: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
