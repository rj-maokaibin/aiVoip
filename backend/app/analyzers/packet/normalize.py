from __future__ import annotations

import re
from typing import Any, Iterable

from .types import NormalizedPacket, RtcpData, RtpData, SdpData, SipData

_KEY_RE = re.compile(r"[^a-z0-9]+")


def _norm_key(value: str) -> str:
    return _KEY_RE.sub("_", value.lower()).strip("_")


def _first(value: Any) -> Any:
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _to_int(value: Any) -> int | None:
    value = _first(value)
    if value is None or value == "":
        return None
    try:
        text = str(value).strip()
        return int(text, 0)
    except (ValueError, TypeError):
        try:
            return int(float(str(value)))
        except (ValueError, TypeError):
            return None


def _to_float(value: Any) -> float | None:
    value = _first(value)
    try:
        return float(value) if value not in (None, "") else None
    except (ValueError, TypeError):
        return None


def _to_bool(value: Any) -> bool | None:
    value = _first(value)
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "set"}


def _flatten(obj: Any, prefix: str = "") -> dict[str, list[Any]]:
    out: dict[str, list[Any]] = {}

    def visit(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for k, v in value.items():
                visit(v, f"{path}.{k}" if path else str(k))
        elif isinstance(value, list):
            for item in value:
                visit(item, path)
        else:
            key = _norm_key(path)
            out.setdefault(key, []).append(value)

    visit(obj, prefix)
    return out


class FieldIndex:
    """Best-effort field lookup across Wireshark/TShark output versions.

    Wireshark EK keys commonly repeat the protocol name (e.g. sip_sip_call_id).
    The normalizer deliberately accepts exact and suffix matches so the semantic
    engines are insulated from dissector output naming changes.
    """

    def __init__(self, layers: dict[str, Any]):
        self.layers = layers
        self.flat = _flatten(layers)

    def get_all(self, *aliases: str) -> list[Any]:
        candidates = [_norm_key(a) for a in aliases]
        for alias in candidates:
            if alias in self.flat:
                return self.flat[alias]
        for alias in candidates:
            suffix = f"_{alias}"
            for key, values in self.flat.items():
                if key.endswith(suffix) or key.endswith(alias):
                    return values
        return []

    def get(self, *aliases: str) -> Any:
        values = self.get_all(*aliases)
        return values[0] if values else None

    def has_layer(self, name: str) -> bool:
        target = _norm_key(name)
        return any(_norm_key(k) == target for k in self.layers)


def _reason_from_status_line(status_line: Any) -> str | None:
    if not status_line:
        return None
    text = str(_first(status_line))
    parts = text.split(None, 2)
    return parts[2].strip() if len(parts) >= 3 else None


def _request_uri(request_line: Any) -> str | None:
    if not request_line:
        return None
    text = str(_first(request_line))
    parts = text.split()
    return parts[1] if len(parts) >= 2 else None


def _normalize_hex_payload(value: Any) -> str | None:
    value = _first(value)
    if value in (None, ""):
        return None
    text = re.sub(r"[^0-9a-fA-F]", "", str(value)).lower()
    return text if text and len(text) % 2 == 0 else None


def _parse_payload_types(values: Iterable[Any]) -> list[int]:
    out: list[int] = []
    for value in values:
        for token in re.findall(r"\b\d{1,3}\b", str(value)):
            number = int(token)
            if 0 <= number <= 127 and number not in out:
                out.append(number)
    return out


def normalize_ek_record(record: dict[str, Any]) -> NormalizedPacket | None:
    layers = record.get("layers") or record.get("_source", {}).get("layers")
    if not isinstance(layers, dict):
        return None
    idx = FieldIndex(layers)

    frame_number = _to_int(idx.get("frame.number", "frame_frame_number"))
    ts = _to_float(idx.get("frame.time_epoch", "frame_frame_time_epoch"))
    if frame_number is None or ts is None:
        return None

    src_ip = idx.get("ip.src", "ip_ip_src", "ipv6.src", "ipv6_ipv6_src")
    dst_ip = idx.get("ip.dst", "ip_ip_dst", "ipv6.dst", "ipv6_ipv6_dst")
    udp_src = _to_int(idx.get("udp.srcport", "udp_udp_srcport"))
    udp_dst = _to_int(idx.get("udp.dstport", "udp_udp_dstport"))
    tcp_src = _to_int(idx.get("tcp.srcport", "tcp_tcp_srcport"))
    tcp_dst = _to_int(idx.get("tcp.dstport", "tcp_tcp_dstport"))
    transport = "UDP" if udp_src is not None else "TCP" if tcp_src is not None else None
    src_port = udp_src if udp_src is not None else tcp_src
    dst_port = udp_dst if udp_dst is not None else tcp_dst

    request_line = idx.get("sip.request_line", "sip_sip_request_line")
    status_line = idx.get("sip.status_line", "sip_sip_status_line")
    method = idx.get("sip.method", "sip_sip_method")
    status_code = _to_int(idx.get("sip.status_code", "sip_sip_status_code"))
    if method is None and request_line:
        method = str(request_line).split()[0]
    sip = None
    if idx.has_layer("sip") or method or status_code is not None:
        sip = SipData(
            method=str(method) if method else None,
            status_code=status_code,
            reason_phrase=_reason_from_status_line(status_line),
            call_id=_first(idx.get("sip.call_id", "sip_sip_call_id")),
            cseq=_to_int(idx.get("sip.cseq.seq", "sip_sip_cseq_seq", "sip.cseq")),
            cseq_method=_first(idx.get("sip.cseq.method", "sip_sip_cseq_method")),
            from_uri=_first(idx.get("sip.from.addr", "sip_sip_from_addr", "sip.from")),
            to_uri=_first(idx.get("sip.to.addr", "sip_sip_to_addr", "sip.to")),
            from_tag=_first(idx.get("sip.from.tag", "sip_sip_from_tag")),
            to_tag=_first(idx.get("sip.to.tag", "sip_sip_to_tag")),
            via_branch=_first(idx.get("sip.via.branch", "sip_sip_via_branch")),
            contact=_first(idx.get("sip.contact", "sip_sip_contact")),
            content_type=_first(idx.get("sip.content_type", "sip_sip_content_type")),
            request_uri=_request_uri(request_line),
            raw_start_line=str(request_line or status_line) if (request_line or status_line) else None,
        )

    sdp = None
    if idx.has_layer("sdp"):
        attrs = [str(x) for x in idx.get_all("sdp.attribute", "sdp_sdp_attribute", "sdp.media_attribute_field")]
        conn = _first(idx.get("sdp.connection_info.address", "sdp_sdp_connection_info_address", "sdp.connection_address"))
        media_port = _to_int(idx.get("sdp.media.port", "sdp_sdp_media_port"))
        media_protocol = _first(idx.get("sdp.media.proto", "sdp_sdp_media_proto", "sdp.media_protocol"))
        payloads = _parse_payload_types(idx.get_all("sdp.media.format", "sdp_sdp_media_format", "sdp.media"))
        raw = _first(idx.get("sdp.raw", "sdp_sdp_raw"))
        sdp = SdpData(
            raw=str(raw) if raw else None,
            connection_address=str(conn) if conn else None,
            media_port=media_port,
            media_protocol=str(media_protocol) if media_protocol else None,
            media_payload_types=payloads,
            attributes=attrs,
        )

    rtp = None
    if idx.has_layer("rtp"):
        rtp = RtpData(
            ssrc=_to_int(idx.get("rtp.ssrc", "rtp_rtp_ssrc")),
            sequence=_to_int(idx.get("rtp.seq", "rtp_rtp_seq")),
            timestamp=_to_int(idx.get("rtp.timestamp", "rtp_rtp_timestamp")),
            payload_type=_to_int(idx.get("rtp.p_type", "rtp_rtp_p_type", "rtp.payload_type")),
            marker=_to_bool(idx.get("rtp.marker", "rtp_rtp_marker")),
            payload_size=_to_int(idx.get("rtp.payload_len", "rtp_rtp_payload_len", "udp.length")),
            payload_hex=_normalize_hex_payload(idx.get("rtp.payload", "rtp_rtp_payload")),
        )

    rtcp = None
    if idx.has_layer("rtcp"):
        rtcp = RtcpData(
            packet_type=str(_first(idx.get("rtcp.packet_type", "rtcp_rtcp_packet_type")) or "") or None,
            ssrc=_to_int(idx.get("rtcp.ssrc.identifier", "rtcp_rtcp_ssrc_identifier", "rtcp.ssrc")),
            fraction_lost=_to_float(idx.get("rtcp.ssrc.fraction", "rtcp_rtcp_ssrc_fraction", "rtcp.fraction_lost")),
            cumulative_lost=_to_int(idx.get("rtcp.ssrc.cum_nr", "rtcp_rtcp_ssrc_cum_nr", "rtcp.cumulative_lost")),
            jitter=_to_int(idx.get("rtcp.ssrc.jitter", "rtcp_rtcp_ssrc_jitter", "rtcp.jitter")),
            lsr=_to_int(idx.get("rtcp.ssrc.lsr", "rtcp_rtcp_ssrc_lsr")),
            dlsr=_to_int(idx.get("rtcp.ssrc.dlsr", "rtcp_rtcp_ssrc_dlsr")),
        )

    protocols = [name for name in ("sip", "sdp", "rtp", "rtcp") if idx.has_layer(name)]
    return NormalizedPacket(
        frame_number=frame_number,
        timestamp=ts,
        src_ip=str(src_ip) if src_ip else None,
        dst_ip=str(dst_ip) if dst_ip else None,
        transport=transport,
        src_port=src_port,
        dst_port=dst_port,
        protocols=protocols,
        sip=sip,
        sdp=sdp,
        rtp=rtp,
        rtcp=rtcp,
        raw_fields={},
    )
