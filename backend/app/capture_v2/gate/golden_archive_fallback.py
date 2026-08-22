from __future__ import annotations

import json
import re
import shutil
import tarfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from app.analyzers.packet.pcap_rtp_fallback import read_rtp_packets_fallback
from app.analyzers.pcm.pcap_udp import PcapFormatError, iter_udp_datagrams
from app.capture_v2.gate.golden_archive_analyze import _rtp_continuity, _safe_member_name
from app.capture_v2.gate.golden_archive_recover import (
    _LOCAL_ROOT as RECOVERY_ROOT,
    archive_name_for,
    inspect_archive,
    sha256_file,
)

_FALLBACK_ROOT = Path("/tmp/capture-v2-golden-fallback")
_SIP_METHODS = {
    "INVITE", "ACK", "BYE", "CANCEL", "REGISTER", "OPTIONS", "INFO",
    "PRACK", "UPDATE", "REFER", "NOTIFY", "SUBSCRIBE", "MESSAGE",
}
_SIGNAL_RE = re.compile(r"(?im)^\s*Signal\s*=\s*([^\r\n; ]+)")
_AUDIO_RE = re.compile(r"(?im)^m=audio\s+(\d+)\b")


def _decode_sip(payload: bytes) -> dict[str, Any] | None:
    """Parse only evidence fields needed by historical Golden reconciliation.

    This is deliberately not a general SIP stack. It recognizes plaintext SIP
    carried in UDP and preserves its limitations in the caller's output.
    """
    if not payload:
        return None
    try:
        text = payload.decode("utf-8", errors="replace")
    except Exception:
        return None
    head, sep, body = text.partition("\r\n\r\n")
    if not sep:
        head, sep, body = text.partition("\n\n")
    lines = [line.rstrip("\r") for line in head.splitlines()]
    if not lines:
        return None
    start = lines[0].strip()
    method: str | None = None
    status_code: int | None = None
    request_target: str | None = None
    if start.startswith("SIP/2.0 "):
        parts = start.split()
        if len(parts) >= 2 and parts[1].isdigit():
            status_code = int(parts[1])
    else:
        parts = start.split()
        if len(parts) < 3 or parts[0].upper() not in _SIP_METHODS or not parts[-1].startswith("SIP/2.0"):
            return None
        method = parts[0].upper()
        request_target = parts[1]

    headers: dict[str, str] = {}
    current: str | None = None
    for raw in lines[1:]:
        if raw[:1] in {" ", "\t"} and current:
            headers[current] = headers[current] + " " + raw.strip()
            continue
        if ":" not in raw:
            continue
        key, value = raw.split(":", 1)
        current = key.strip().lower()
        headers[current] = value.strip()

    call_id = headers.get("call-id") or headers.get("i")
    content_type = (headers.get("content-type") or headers.get("c") or "").lower()
    cseq = headers.get("cseq", "")
    cseq_method = cseq.split()[-1].upper() if cseq.split() else None
    media_ports = [int(match.group(1)) for match in _AUDIO_RE.finditer(body)] if "sdp" in content_type else []
    signals = [match.group(1) for match in _SIGNAL_RE.finditer(body)] if method == "INFO" else []
    return {
        "method": method,
        "status_code": status_code,
        "request_target": request_target,
        "call_id": call_id,
        "cseq_method": cseq_method,
        "content_type": content_type or None,
        "sdp_audio_ports": media_ports,
        "info_signals": signals,
    }


def _materialize(tf: tarfile.TarFile, names: list[str], root: Path) -> list[tuple[str, Path]]:
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
    result: list[tuple[str, Path]] = []
    for index, name in enumerate(sorted(names)):
        if not _safe_member_name(name):
            raise ValueError("GOLDEN_ARCHIVE_UNSAFE_MEMBER")
        member = tf.getmember(name)
        if not member.isfile():
            continue
        stream = tf.extractfile(member)
        if stream is None:
            continue
        local = root / f"{index:03d}_{Path(name).name}"
        with local.open("wb") as out:
            shutil.copyfileobj(stream, out, length=1024 * 1024)
        result.append((name, local))
    return result


def analyze_archive_fallback(*, device_id: str, model: str, archive_date: str) -> dict[str, Any]:
    archive_name = archive_name_for(model, archive_date)
    archive_path = RECOVERY_ROOT / device_id / archive_date / archive_name
    if not archive_path.is_file():
        return {
            "verdict": "INCONCLUSIVE",
            "reason": "GOLDEN_ARCHIVE_NOT_RECOVERED",
            "parser": "PURE_PYTHON_CLASSIC_PCAP",
        }
    inventory = inspect_archive(archive_path)
    if any(name.lower().endswith(".pcapng") for name in inventory["pcap_names"]):
        return {
            "verdict": "INCONCLUSIVE",
            "reason": "FALLBACK_CLASSIC_PCAP_ONLY",
            "parser": "PURE_PYTHON_CLASSIC_PCAP",
        }

    archive_sha = sha256_file(archive_path)
    local_root = _FALLBACK_ROOT / device_id / archive_date / archive_sha[:16]
    with tarfile.open(archive_path, "r:gz") as tf:
        materialized = _materialize(tf, list(inventory["pcap_names"]), local_root / "pcaps")

    sip_methods: Counter[str] = Counter()
    sip_status_codes: Counter[str] = Counter()
    call_ids: Counter[str] = Counter()
    request_targets: Counter[str] = Counter()
    sdp_audio_ports: Counter[str] = Counter()
    info_signals: list[dict[str, Any]] = []
    sip_packet_count = 0
    first_sip_time: float | None = None
    last_sip_time: float | None = None
    udp_errors: list[dict[str, str]] = []

    for segment_name, path in materialized:
        try:
            for datagram in iter_udp_datagrams(path):
                if datagram.src_port not in {5060, 5061} and datagram.dst_port not in {5060, 5061}:
                    # Some deployments use non-standard SIP UDP ports; still allow
                    # strong payload recognition below without accepting arbitrary
                    # binary data as SIP.
                    if not datagram.payload.startswith(tuple((m + " ").encode() for m in _SIP_METHODS)) and not datagram.payload.startswith(b"SIP/2.0 "):
                        continue
                parsed = _decode_sip(datagram.payload)
                if parsed is None:
                    continue
                sip_packet_count += 1
                first_sip_time = datagram.timestamp if first_sip_time is None else min(first_sip_time, datagram.timestamp)
                last_sip_time = datagram.timestamp if last_sip_time is None else max(last_sip_time, datagram.timestamp)
                if parsed["method"]:
                    sip_methods[parsed["method"]] += 1
                if parsed["status_code"] is not None:
                    sip_status_codes[str(parsed["status_code"])] += 1
                if parsed["call_id"]:
                    call_ids[parsed["call_id"]] += 1
                if parsed["request_target"]:
                    request_targets[parsed["request_target"]] += 1
                for port in parsed["sdp_audio_ports"]:
                    sdp_audio_ports[str(port)] += 1
                for signal in parsed["info_signals"]:
                    info_signals.append({
                        "source_time": datagram.timestamp,
                        "signal": signal,
                        "call_id": parsed["call_id"],
                        "segment": segment_name,
                    })
        except (PcapFormatError, OSError) as exc:
            udp_errors.append({"segment": segment_name, "error": str(exc)})

    rtp_records: dict[tuple[Any, ...], list[tuple[float, int, str]]] = defaultdict(list)
    rtp_reader_errors: list[dict[str, str]] = []
    for segment_name, path in materialized:
        try:
            packets = read_rtp_packets_fallback(
                path,
                exclude_ports={40000, 50000, 5060, 5061},
                min_packets=20,
            )
            for packet in packets:
                if packet.rtp is None or packet.rtp.sequence is None:
                    continue
                key = (
                    packet.src_ip,
                    packet.src_port,
                    packet.dst_ip,
                    packet.dst_port,
                    packet.rtp.ssrc,
                    packet.rtp.payload_type,
                )
                rtp_records[key].append((packet.timestamp, int(packet.rtp.sequence), segment_name))
        except (PcapFormatError, OSError, ValueError) as exc:
            rtp_reader_errors.append({"segment": segment_name, "error": str(exc)})

    streams: list[dict[str, Any]] = []
    total_packets = total_missing = total_cross_missing = total_reordered = 0
    first_rtp_time: float | None = None
    last_rtp_time: float | None = None
    for key, records in sorted(rtp_records.items(), key=lambda item: str(item[0])):
        continuity = _rtp_continuity(records)
        total_packets += int(continuity["packet_count"])
        total_missing += int(continuity["estimated_missing_packets"])
        total_cross_missing += int(continuity["cross_segment_missing_packets"])
        total_reordered += int(continuity["backward_or_reordered_events"])
        times = [row[0] for row in records]
        if times:
            first_rtp_time = min(times) if first_rtp_time is None else min(first_rtp_time, min(times))
            last_rtp_time = max(times) if last_rtp_time is None else max(last_rtp_time, max(times))
        streams.append({
            "src_ip": key[0],
            "src_port": key[1],
            "dst_ip": key[2],
            "dst_port": key[3],
            "ssrc": key[4],
            "payload_type": key[5],
            **continuity,
        })

    return {
        "verdict": "PASS",
        "reason": "GOLDEN_ARCHIVE_FALLBACK_ANALYSIS_COMPLETED",
        "release_gate_effect": "EVIDENCE_ONLY_NOT_R5_PASS",
        "parser": "PURE_PYTHON_CLASSIC_PCAP",
        "limitations": [
            "Ethernet/VLAN + IPv4 + UDP only",
            "plaintext SIP/UDP only; SIP/TCP/TLS is not covered",
            "RTP is accepted only after per-segment continuity heuristics with >=20 packets",
            "no RTCP or full SDP semantic reconstruction in fallback mode",
        ],
        "archive": {
            "name": archive_name,
            "sha256": archive_sha,
            "pcap_count": inventory["pcap_count"],
        },
        "sip": {
            "packet_count": sip_packet_count,
            "methods": dict(sip_methods.most_common()),
            "status_codes": dict(sip_status_codes.most_common()),
            "call_ids": dict(call_ids.most_common()),
            "request_targets": dict(request_targets.most_common()),
            "sdp_audio_ports": dict(sdp_audio_ports.most_common()),
            "info_signals": sorted(info_signals, key=lambda item: item["source_time"]),
            "first_source_time": first_sip_time,
            "last_source_time": last_sip_time,
        },
        "rtp": {
            "packet_count": total_packets,
            "stream_count": len(streams),
            "estimated_missing_packets": total_missing,
            "cross_segment_missing_packets": total_cross_missing,
            "backward_or_reordered_events": total_reordered,
            "first_source_time": first_rtp_time,
            "last_source_time": last_rtp_time,
            "streams": streams,
        },
        "errors": {
            "udp": udp_errors,
            "rtp": rtp_reader_errors,
        },
        "evidence_checks": {
            "sip_udp_present": sip_packet_count > 0,
            "sip_call_id_present": bool(call_ids),
            "rtp_present": total_packets > 0,
            "rtp_estimated_missing_zero": total_missing == 0 if total_packets else None,
            "rtp_cross_segment_missing_zero": total_cross_missing == 0 if total_packets else None,
            "reader_complete": not udp_errors and not rtp_reader_errors,
        },
    }


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Pure-Python fallback analysis for a recovered Golden archive")
    parser.add_argument("--device-id", required=True)
    parser.add_argument("--model", choices=["APF1250", "APF3260-M"], required=True)
    parser.add_argument("--archive-date", required=True)
    args = parser.parse_args()
    try:
        payload = analyze_archive_fallback(
            device_id=args.device_id,
            model=args.model,
            archive_date=args.archive_date,
        )
    except Exception as exc:
        payload = {"verdict": "FAIL", "reason": f"{type(exc).__name__}:{exc}"}
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return 2
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0 if payload.get("verdict") == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
