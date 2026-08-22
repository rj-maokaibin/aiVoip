from __future__ import annotations

import argparse
import json
import re
import shutil
import tarfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from app.analyzers.packet.tshark import TSharkAdapter, TSharkAnalysisError, TSharkUnavailable
from app.analyzers.pcm.pcap_udp import PcapFormatError, iter_udp_datagrams
from app.capture_v2.gate.golden_archive_recover import (
    _LOCAL_ROOT as RECOVERY_ROOT,
    archive_name_for,
    inspect_archive,
    sha256_file,
)

_ANALYSIS_ROOT = Path("/tmp/capture-v2-golden-analysis")
_PCM_PORTS = (40000, 50000)
_TCPDUMP_PATTERNS = {
    "packets_captured": re.compile(r"(?m)^\s*(\d+)\s+packets captured\s*$"),
    "packets_received_by_filter": re.compile(r"(?m)^\s*(\d+)\s+packets received by filter\s*$"),
    "packets_dropped_by_kernel": re.compile(r"(?m)^\s*(\d+)\s+packets dropped by kernel\s*$"),
}


def _safe_member_name(name: str) -> bool:
    p = Path(name)
    return bool(name) and not name.startswith("/") and ".." not in p.parts


def parse_tcpdump_stats(text: str) -> dict[str, int | None]:
    result: dict[str, int | None] = {}
    for key, pattern in _TCPDUMP_PATTERNS.items():
        match = pattern.search(text or "")
        result[key] = int(match.group(1)) if match else None
    return result


def _rtp_continuity(records: list[tuple[float, int, str]]) -> dict[str, Any]:
    """Calculate deterministic sequence continuity across rotated PCAP segments.

    Records are (source_time, sequence, segment_name). Sequence wrap 65535->0 is
    naturally accepted because the delta is calculated modulo 65536. Large
    backwards/reordered jumps are reported separately and are never converted
    into a huge synthetic packet-loss count.
    """
    rows = sorted(records, key=lambda row: (row[0], row[2]))
    missing = duplicates = backwards = cross_segment_transitions = cross_segment_gaps = 0
    max_forward_gap = 0
    previous: tuple[float, int, str] | None = None
    for current in rows:
        if previous is not None:
            delta = (current[1] - previous[1]) & 0xFFFF
            changed_segment = current[2] != previous[2]
            if changed_segment:
                cross_segment_transitions += 1
            if delta == 0:
                duplicates += 1
            elif delta == 1:
                pass
            elif delta < 32768:
                gap = delta - 1
                missing += gap
                max_forward_gap = max(max_forward_gap, gap)
                if changed_segment:
                    cross_segment_gaps += gap
            else:
                backwards += 1
        previous = current
    return {
        "packet_count": len(rows),
        "estimated_missing_packets": missing,
        "duplicate_sequence_events": duplicates,
        "backward_or_reordered_events": backwards,
        "cross_segment_transitions": cross_segment_transitions,
        "cross_segment_missing_packets": cross_segment_gaps,
        "max_forward_gap": max_forward_gap,
    }


def _read_text_member(tf: tarfile.TarFile, name: str) -> str:
    try:
        member = tf.getmember(name)
    except KeyError:
        return ""
    if not member.isfile() or not _safe_member_name(member.name):
        return ""
    fh = tf.extractfile(member)
    if fh is None:
        return ""
    return fh.read().decode("utf-8", errors="replace")


def _materialize_pcaps(tf: tarfile.TarFile, names: list[str], output_dir: Path) -> list[tuple[str, Path]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    materialized: list[tuple[str, Path]] = []
    for index, name in enumerate(sorted(names)):
        if not _safe_member_name(name):
            raise ValueError("GOLDEN_ARCHIVE_UNSAFE_MEMBER")
        member = tf.getmember(name)
        if not member.isfile():
            continue
        fh = tf.extractfile(member)
        if fh is None:
            continue
        suffix = Path(name).suffix.lower()
        local = output_dir / f"{index:03d}_{Path(name).stem}{suffix}"
        with local.open("wb") as out:
            shutil.copyfileobj(fh, out, length=1024 * 1024)
        materialized.append((name, local))
    return materialized


def analyze_archive(*, device_id: str, model: str, archive_date: str) -> dict[str, Any]:
    archive_name = archive_name_for(model, archive_date)
    archive_path = RECOVERY_ROOT / device_id / archive_date / archive_name
    if not archive_path.is_file():
        return {
            "verdict": "INCONCLUSIVE",
            "reason": "GOLDEN_ARCHIVE_NOT_RECOVERED",
            "archive_name": archive_name,
            "expected_local_path": str(archive_path),
        }

    archive_sha = sha256_file(archive_path)
    inventory = inspect_archive(archive_path)
    analysis_dir = _ANALYSIS_ROOT / device_id / archive_date / archive_sha[:16]
    pcap_dir = analysis_dir / "pcaps"
    if pcap_dir.exists():
        shutil.rmtree(pcap_dir)

    with tarfile.open(archive_path, "r:gz") as tf:
        names = [m.name for m in tf.getmembers()]
        unsafe = [name for name in names if not _safe_member_name(name)]
        if unsafe:
            raise ValueError("GOLDEN_ARCHIVE_UNSAFE_MEMBER")
        stderr_name = next((name for name in names if name.endswith("/tcpdump.stderr") or name == "tcpdump.stderr"), "")
        stdout_name = next((name for name in names if name.endswith("/tcpdump.stdout") or name == "tcpdump.stdout"), "")
        tcpdump_stderr = _read_text_member(tf, stderr_name) if stderr_name else ""
        tcpdump_stdout = _read_text_member(tf, stdout_name) if stdout_name else ""
        materialized = _materialize_pcaps(tf, list(inventory["pcap_names"]), pcap_dir)

    tcpdump_stats = parse_tcpdump_stats(tcpdump_stderr + "\n" + tcpdump_stdout)

    pcm: dict[int, dict[str, Any]] = {
        port: {
            "packet_count": 0,
            "payload_bytes": 0,
            "first_source_time": None,
            "last_source_time": None,
            "flows": Counter(),
        }
        for port in _PCM_PORTS
    }
    udp_reader_errors: list[dict[str, str]] = []
    for segment_name, local_path in materialized:
        if local_path.suffix.lower() != ".pcap":
            udp_reader_errors.append({"segment": segment_name, "error": "PCM_READER_CLASSIC_PCAP_ONLY"})
            continue
        try:
            for datagram in iter_udp_datagrams(local_path):
                matched = {port for port in _PCM_PORTS if datagram.src_port == port or datagram.dst_port == port}
                for port in matched:
                    stat = pcm[port]
                    stat["packet_count"] += 1
                    stat["payload_bytes"] += len(datagram.payload)
                    stat["first_source_time"] = (
                        datagram.timestamp
                        if stat["first_source_time"] is None
                        else min(stat["first_source_time"], datagram.timestamp)
                    )
                    stat["last_source_time"] = (
                        datagram.timestamp
                        if stat["last_source_time"] is None
                        else max(stat["last_source_time"], datagram.timestamp)
                    )
                    flow = f"{datagram.src_ip}:{datagram.src_port}->{datagram.dst_ip}:{datagram.dst_port}"
                    stat["flows"][flow] += 1
        except (PcapFormatError, OSError) as exc:
            udp_reader_errors.append({"segment": segment_name, "error": str(exc)})

    for port in _PCM_PORTS:
        pcm[port]["flows"] = dict(pcm[port]["flows"].most_common(20))

    tshark = TSharkAdapter(timeout_seconds=120)
    tshark_version: str | None = None
    tshark_errors: list[dict[str, str]] = []
    sip_methods: Counter[str] = Counter()
    sip_status_codes: Counter[str] = Counter()
    call_ids: Counter[str] = Counter()
    sdp_media_ports: Counter[str] = Counter()
    rtp_records: dict[tuple[Any, ...], list[tuple[float, int, str]]] = defaultdict(list)
    voip_packet_count = 0
    sip_packet_count = 0
    rtp_packet_count = 0
    first_voip_source_time: float | None = None
    last_voip_source_time: float | None = None

    try:
        tshark_version = tshark.version()
        for segment_name, local_path in materialized:
            try:
                for packet in tshark.iter_packets(local_path):
                    voip_packet_count += 1
                    first_voip_source_time = packet.timestamp if first_voip_source_time is None else min(first_voip_source_time, packet.timestamp)
                    last_voip_source_time = packet.timestamp if last_voip_source_time is None else max(last_voip_source_time, packet.timestamp)
                    if packet.sip is not None:
                        sip_packet_count += 1
                        if packet.sip.method:
                            sip_methods[packet.sip.method] += 1
                        if packet.sip.status_code is not None:
                            sip_status_codes[str(packet.sip.status_code)] += 1
                        if packet.sip.call_id:
                            call_ids[packet.sip.call_id] += 1
                    if packet.sdp is not None and packet.sdp.media_port is not None:
                        sdp_media_ports[str(packet.sdp.media_port)] += 1
                    if packet.rtp is not None and packet.rtp.sequence is not None:
                        rtp_packet_count += 1
                        key = (
                            packet.src_ip,
                            packet.src_port,
                            packet.dst_ip,
                            packet.dst_port,
                            packet.rtp.ssrc,
                            packet.rtp.payload_type,
                        )
                        rtp_records[key].append((packet.timestamp, int(packet.rtp.sequence), segment_name))
            except (TSharkAnalysisError, OSError) as exc:
                tshark_errors.append({"segment": segment_name, "error": str(exc)})
    except TSharkUnavailable as exc:
        tshark_errors.append({"segment": "<environment>", "error": str(exc)})

    stream_summaries: list[dict[str, Any]] = []
    total_missing = total_cross_segment_missing = total_reordered = 0
    for key, records in sorted(rtp_records.items(), key=lambda item: str(item[0])):
        continuity = _rtp_continuity(records)
        total_missing += int(continuity["estimated_missing_packets"])
        total_cross_segment_missing += int(continuity["cross_segment_missing_packets"])
        total_reordered += int(continuity["backward_or_reordered_events"])
        stream_summaries.append({
            "src_ip": key[0],
            "src_port": key[1],
            "dst_ip": key[2],
            "dst_port": key[3],
            "ssrc": key[4],
            "payload_type": key[5],
            **continuity,
        })

    kernel_drop = tcpdump_stats["packets_dropped_by_kernel"]
    evidence_checks = {
        "tcpdump_kernel_drop_known": kernel_drop is not None,
        "tcpdump_kernel_drop_zero": kernel_drop == 0 if kernel_drop is not None else None,
        "sip_present": sip_packet_count > 0,
        "sip_call_id_present": bool(call_ids),
        "rtp_present": rtp_packet_count > 0,
        "rtp_estimated_missing_zero": total_missing == 0 if rtp_packet_count > 0 else None,
        "rtp_cross_segment_missing_zero": total_cross_segment_missing == 0 if rtp_packet_count > 0 else None,
        "pcm_40000_present": pcm[40000]["packet_count"] > 0,
        "pcm_50000_present": pcm[50000]["packet_count"] > 0,
        "tshark_complete": not tshark_errors and tshark_version is not None,
        "classic_pcap_udp_reader_complete": not udp_reader_errors,
    }
    archive_evidence_complete = all(value is True for value in evidence_checks.values())

    return {
        "verdict": "PASS",
        "reason": "GOLDEN_ARCHIVE_ANALYSIS_COMPLETED",
        "release_gate_effect": "EVIDENCE_ONLY_NOT_R5_PASS",
        "archive": {
            "name": archive_name,
            "local_path": str(archive_path),
            "size_bytes": archive_path.stat().st_size,
            "sha256": archive_sha,
            "inventory": inventory,
        },
        "analysis_dir": str(analysis_dir),
        "tcpdump": tcpdump_stats,
        "pcm": {str(port): pcm[port] for port in _PCM_PORTS},
        "voip": {
            "tshark_version": tshark_version,
            "packet_count": voip_packet_count,
            "sip_packet_count": sip_packet_count,
            "sip_methods": dict(sip_methods.most_common()),
            "sip_status_codes": dict(sip_status_codes.most_common()),
            "call_ids": dict(call_ids.most_common()),
            "sdp_media_ports": dict(sdp_media_ports.most_common()),
            "rtp_packet_count": rtp_packet_count,
            "rtp_stream_count": len(stream_summaries),
            "rtp_estimated_missing_packets": total_missing,
            "rtp_cross_segment_missing_packets": total_cross_segment_missing,
            "rtp_backward_or_reordered_events": total_reordered,
            "rtp_streams": stream_summaries,
            "first_source_time": first_voip_source_time,
            "last_source_time": last_voip_source_time,
        },
        "analysis_errors": {
            "tshark": tshark_errors,
            "udp_reader": udp_reader_errors,
        },
        "evidence_checks": evidence_checks,
        "archive_evidence_complete": archive_evidence_complete,
        "remaining_release_gap": "FXS_GROUND_TRUTH_RECONCILIATION_REQUIRED",
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Analyze a previously recovered historical V2.1 Golden archive")
    parser.add_argument("--device-id", required=True)
    parser.add_argument("--model", choices=["APF1250", "APF3260-M"], required=True)
    parser.add_argument("--archive-date", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        payload = analyze_archive(device_id=args.device_id, model=args.model, archive_date=args.archive_date)
    except (ValueError, tarfile.TarError, OSError) as exc:
        payload = {"verdict": "FAIL", "reason": str(exc), "release_gate_effect": "EVIDENCE_ONLY_NOT_R5_PASS"}
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return 2
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0 if payload.get("verdict") == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
