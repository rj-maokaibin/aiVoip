from __future__ import annotations

from typing import Any

from app.golden.offline_analysis_e2e import GoldenCheck


def validate_extended_offline_truth(bundle: dict, manifest: dict) -> list[GoldenCheck]:
    checks: list[GoldenCheck] = []
    expected = manifest.get("expected") or {}

    def add(name: str, passed: bool, actual: Any, wanted: Any, category: str) -> None:
        checks.append(GoldenCheck(name, bool(passed), actual, wanted, category))

    pcm = bundle.get("pcm") or {}
    pcm_exp = expected.get("pcm") or {}
    pcm_summary = pcm.get("summary") or {}
    pcm_format = pcm.get("format") or {}
    add("pcm.total_packets", int(pcm_summary.get("total_packets") or 0) == int(pcm_exp.get("total_packets") or 0), pcm_summary.get("total_packets"), pcm_exp.get("total_packets"), "PCM")
    for key, wanted in (pcm_exp.get("format") or {}).items():
        actual = pcm_format.get(key)
        if key == "endian":
            passed = str(actual or "").lower() in {str(wanted).lower(), "le" if str(wanted).lower() == "little" else str(wanted).lower()}
        else:
            passed = actual == wanted
        add(f"pcm.format.{key}", passed, actual, wanted, "PCM")

    by_tap = {str((s.get("tap") or {}).get("name")): s for s in pcm.get("streams", []) or []}
    source_ips: set[str] = set()
    for tap_name, tap_exp in (pcm_exp.get("taps") or {}).items():
        stream = by_tap.get(tap_name)
        add(f"pcm.tap.{tap_name}.exists", stream is not None, bool(stream), True, "PCM")
        if not stream:
            continue
        tap = stream.get("tap") or {}
        add(f"pcm.tap.{tap_name}.direction", str(tap.get("direction") or "").upper() == str(tap_exp.get("direction") or "").upper(), tap.get("direction"), tap_exp.get("direction"), "PCM")
        add(f"pcm.tap.{tap_name}.packet_count", int(stream.get("packet_count") or 0) == int(tap_exp.get("packet_count") or 0), stream.get("packet_count"), tap_exp.get("packet_count"), "PCM")
        for endpoint in stream.get("source_endpoints", []) or []:
            if endpoint.get("ip"):
                source_ips.add(str(endpoint["ip"]))
    expected_source_ip = pcm_exp.get("source_device_ip")
    if expected_source_ip:
        add("pcm.source_device_ip", source_ips == {str(expected_source_ip)}, sorted(source_ips), [str(expected_source_ip)], "PCM_PROVENANCE")

    rtp_exp = (expected.get("rtp") or {}).get("primary_uplink") or {}
    primary = next((s for s in (bundle.get("packet") or {}).get("rtp_streams", []) or [] if s.get("src_ip") == rtp_exp.get("src_ip") and int(s.get("src_port") or 0) == int(rtp_exp.get("src_port") or 0) and s.get("dst_ip") == rtp_exp.get("dst_ip") and int(s.get("dst_port") or 0) == int(rtp_exp.get("dst_port") or 0)), None)
    actual_high_delta = [e for e in (primary or {}).get("events", []) or [] if e.get("type") == "HIGH_DELTA"]
    expected_high_delta = list(rtp_exp.get("high_delta_events") or [])
    actual_high_delta.sort(key=lambda e: float((e.get("details") or {}).get("delta_ms") or 0.0))
    expected_high_delta.sort(key=lambda e: float((e.get("delta_ms") or {}).get("min") or 0.0))
    add("rtp.high_delta.frame_event_count", len(actual_high_delta) == len(expected_high_delta), len(actual_high_delta), len(expected_high_delta), "RTP_FRAME")
    if len(actual_high_delta) == len(expected_high_delta):
        for index, (actual_event, expected_event) in enumerate(zip(actual_high_delta, expected_high_delta), start=1):
            details = actual_event.get("details") or {}
            delta_range = expected_event.get("delta_ms") or {}
            delta = float(details.get("delta_ms") or 0.0)
            add(f"rtp.high_delta.{index}.delta_ms", float(delta_range.get("min") or 0.0) <= delta <= float(delta_range.get("max") or 0.0), delta, delta_range, "RTP_FRAME")
            for field in ("previous_frame_number", "current_frame_number", "previous_sequence", "current_sequence"):
                add(f"rtp.high_delta.{index}.{field}", int(details.get(field) or -1) == int(expected_event.get(field) or -2), details.get(field), expected_event.get(field), "RTP_FRAME")
            prev_seq = details.get("previous_sequence")
            curr_seq = details.get("current_sequence")
            if prev_seq is not None and curr_seq is not None:
                add(f"rtp.high_delta.{index}.sequence_continuity", ((int(curr_seq) - int(prev_seq)) & 0xFFFF) == 1, {"previous": prev_seq, "current": curr_seq}, "current sequence is previous + 1", "RTP_FRAME")

    report = bundle.get("report") or {}
    report_exp = expected.get("report") or {}
    finding_types = [str(f.get("type")) for f in report.get("findings", []) or []]
    for required in report_exp.get("required_finding_types", []) or []:
        add(f"report.required_finding.{required}", required in finding_types, finding_types, f"must contain {required}", "REPORT")

    report_context = report.get("analysis_context") or {}
    bundle_context = bundle.get("analysis_context") or {}
    add("report.analysis_context_consistency", report_context == bundle_context, report_context, bundle_context, "REPORT")
    report_call = report.get("display_call") or report.get("call")
    add("report.call_consistency", report_call == bundle.get("display_call"), report_call, bundle.get("display_call"), "REPORT")

    return checks
