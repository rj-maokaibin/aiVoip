from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Iterable

from .types import SdpData


STATIC_PAYLOADS: dict[int, tuple[str, int]] = {
    0: ("PCMU", 8000),
    3: ("GSM", 8000),
    4: ("G723", 8000),
    8: ("PCMA", 8000),
    9: ("G722", 8000),
    18: ("G729", 8000),
}


@dataclass(slots=True)
class Codec:
    payload_type: int
    name: str
    clock_rate: int
    channels: int = 1
    fmtp: str | None = None


@dataclass(slots=True)
class MediaDescription:
    media: str = "audio"
    port: int | None = None
    protocol: str | None = None
    payload_types: list[int] = field(default_factory=list)
    codecs: list[Codec] = field(default_factory=list)
    direction: str = "sendrecv"
    ptime_ms: float | None = None
    connection_address: str | None = None
    telephone_event_payloads: list[int] = field(default_factory=list)


@dataclass(slots=True)
class ParsedSdp:
    connection_address: str | None = None
    media: list[MediaDescription] = field(default_factory=list)

    def payload_map(self) -> dict[int, tuple[str, int]]:
        result: dict[int, tuple[str, int]] = dict(STATIC_PAYLOADS)
        for item in self.media:
            for codec in item.codecs:
                result[codec.payload_type] = (codec.name, codec.clock_rate)
        return result

    def to_dict(self):
        return asdict(self)


def parse_sdp_text(text: str) -> ParsedSdp:
    parsed = ParsedSdp()
    current: MediaDescription | None = None
    rtpmap: dict[int, Codec] = {}
    fmtp: dict[int, str] = {}

    for raw_line in text.replace("\r", "").split("\n"):
        line = raw_line.strip()
        if not line or len(line) < 2 or line[1] != "=":
            continue
        prefix, value = line[0], line[2:]
        if prefix == "c":
            parts = value.split()
            addr = parts[-1] if parts else None
            if current is None:
                parsed.connection_address = addr
            else:
                current.connection_address = addr
        elif prefix == "m":
            parts = value.split()
            if len(parts) >= 3:
                payloads = []
                for token in parts[3:]:
                    try:
                        payloads.append(int(token))
                    except ValueError:
                        pass
                current = MediaDescription(media=parts[0], port=_safe_int(parts[1]), protocol=parts[2], payload_types=payloads)
                parsed.media.append(current)
        elif prefix == "a":
            lower = value.lower()
            if lower in {"sendrecv", "sendonly", "recvonly", "inactive"} and current:
                current.direction = lower
            elif lower.startswith("ptime:") and current:
                try:
                    current.ptime_ms = float(value.split(":", 1)[1])
                except ValueError:
                    pass
            elif lower.startswith("rtpmap:"):
                rest = value.split(":", 1)[1]
                head, _, spec = rest.partition(" ")
                pt = _safe_int(head)
                parts = spec.split("/")
                if pt is not None and len(parts) >= 2:
                    codec = Codec(pt, parts[0].upper(), _safe_int(parts[1]) or 8000, _safe_int(parts[2]) or 1 if len(parts) >= 3 else 1)
                    rtpmap[pt] = codec
            elif lower.startswith("fmtp:"):
                rest = value.split(":", 1)[1]
                head, _, spec = rest.partition(" ")
                pt = _safe_int(head)
                if pt is not None:
                    fmtp[pt] = spec

    for media in parsed.media:
        for pt in media.payload_types:
            codec = rtpmap.get(pt)
            if codec is None and pt in STATIC_PAYLOADS:
                name, clock = STATIC_PAYLOADS[pt]
                codec = Codec(pt, name, clock)
            if codec:
                if pt in fmtp:
                    codec.fmtp = fmtp[pt]
                media.codecs.append(codec)
                if codec.name.lower() == "telephone-event":
                    media.telephone_event_payloads.append(pt)
        if media.connection_address is None:
            media.connection_address = parsed.connection_address
    return parsed


def parse_sdp(data: SdpData | None) -> ParsedSdp | None:
    if data is None:
        return None
    if data.raw:
        return parse_sdp_text(data.raw)
    media = MediaDescription(
        port=data.media_port,
        protocol=data.media_protocol,
        payload_types=list(data.media_payload_types),
        connection_address=data.connection_address,
    )
    for pt in media.payload_types:
        if pt in STATIC_PAYLOADS:
            name, clock = STATIC_PAYLOADS[pt]
            media.codecs.append(Codec(pt, name, clock))
    for attr in data.attributes:
        lower = attr.lower()
        if lower in {"sendrecv", "sendonly", "recvonly", "inactive"}:
            media.direction = lower
        elif lower.startswith("ptime:"):
            try:
                media.ptime_ms = float(attr.split(":", 1)[1])
            except ValueError:
                pass
    return ParsedSdp(connection_address=data.connection_address, media=[media])


def negotiate_codecs(offer: ParsedSdp | None, answer: ParsedSdp | None) -> list[dict]:
    if not offer or not answer:
        return []
    offer_codecs = {(c.name.upper(), c.clock_rate): c for m in offer.media for c in m.codecs if c.name.lower() != "telephone-event"}
    result = []
    for media in answer.media:
        for codec in media.codecs:
            key = (codec.name.upper(), codec.clock_rate)
            if codec.name.lower() != "telephone-event" and key in offer_codecs:
                result.append(asdict(codec))
    return result


def merge_payload_maps(sdps: Iterable[ParsedSdp]) -> dict[int, tuple[str, int]]:
    result = dict(STATIC_PAYLOADS)
    for sdp in sdps:
        result.update(sdp.payload_map())
    return result


def _safe_int(value: str) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
