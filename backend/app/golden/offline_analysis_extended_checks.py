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

    semantic_exp = rtp_exp.get("high_delta_semantics") or {}
    allowed_classifications = set(semantic_exp.get("allowed_classifications") or [])
    for index, actual_event in enumerate(actual_high_delta, start=1):
        details = actual_event.get("details") or {}
        if semantic_exp.get("required_sequence_continuous"):
            add(f"rtp.high_delta.{index}.semantic.sequence_continuous", details.get("sequence_continuous") is True, details.get("sequence_continuous"), True, "RTP_SEMANTIC")
        required_loss_semantics = semantic_exp.get("required_loss_semantics")
        if required_loss_semantics:
            add(f"rtp.high_delta.{index}.semantic.loss", details.get("loss_semantics") == required_loss_semantics, details.get("loss_semantics"), required_loss_semantics, "RTP_SEMANTIC")
        if allowed_classifications:
            add(f"rtp.high_delta.{index}.semantic.classification", details.get("classification") in allowed_classifications, details.get("classification"), sorted(allowed_classifications), "RTP_SEMANTIC")
        if semantic_exp.get("catch_up_required"):
            catch_up = details.get("catch_up") or {}
            add(f"rtp.high_delta.{index}.semantic.catch_up", catch_up.get("status") in {"PARTIAL", "FULL"} and catch_up.get("observed") is True, catch_up, "PARTIAL or FULL catch-up observed", "RTP_SEMANTIC")

    media = bundle.get("media") or {}
    dtmf_exp = expected.get("dtmf") or {}
    dtmf_matches = [e for e in media.get("cross_layer_events", []) or [] if e.get("type") == dtmf_exp.get("required_event_type")]
    add("dtmf.match_count", len(dtmf_matches) == int(dtmf_exp.get("expected_match_count") or 0), len(dtmf_matches), dtmf_exp.get("expected_match_count"), "CROSS_LAYER")
    if dtmf_matches:
        matched_call_ids = [str((e.get("details") or {}).get("call_id") or (e.get("scope") or {}).get("call_id") or "") for e in dtmf_matches]
        add("dtmf.subject_call_id", matched_call_ids == [str(dtmf_exp.get("call_id"))], matched_call_ids, [str(dtmf_exp.get("call_id"))], "CROSS_LAYER")

    report = bundle.get("report") or {}
    report_exp = expected.get("report") or {}
    findings = report.get("findings", []) or []
    finding_types = [str(f.get("type")) for f in findings]
    for required in report_exp.get("required_finding_types", []) or []:
        add(f"report.required_finding.{required}", required in finding_types, finding_types, f"must contain {required}", "REPORT")

    high_delta_report_exp = report_exp.get("high_delta_primary_stream_finding") or {}
    if high_delta_report_exp:
        primary_stream_id = (primary or {}).get("stream_id")
        finding = next((f for f in findings if f.get("type") == "HIGH_DELTA" and (f.get("scope") or {}).get("rtp_stream_id") == primary_stream_id), None)
        add("report.high_delta.primary_stream.exists", finding is not None, (finding or {}).get("scope"), primary_stream_id, "REPORT_SEMANTIC")
        if finding:
            metrics = finding.get("metrics") or {}
            semantic = finding.get("semantic_summary") or {}
            add("report.high_delta.primary_stream.occurrence_count", int(finding.get("occurrence_count") or 0) == int(high_delta_report_exp.get("occurrence_count") or 0), finding.get("occurrence_count"), high_delta_report_exp.get("occurrence_count"), "REPORT_SEMANTIC")
            add("report.high_delta.primary_stream.event_count", int(metrics.get("event_count") or 0) == int(high_delta_report_exp.get("occurrence_count") or 0), metrics.get("event_count"), high_delta_report_exp.get("occurrence_count"), "REPORT_SEMANTIC")
            add("report.high_delta.primary_stream.sequence", metrics.get("all_sequence_continuous") is bool(high_delta_report_exp.get("all_sequence_continuous")), metrics.get("all_sequence_continuous"), high_delta_report_exp.get("all_sequence_continuous"), "REPORT_SEMANTIC")
            add("report.high_delta.primary_stream.loss_interpretation", semantic.get("loss_interpretation") == high_delta_report_exp.get("loss_interpretation"), semantic.get("loss_interpretation"), high_delta_report_exp.get("loss_interpretation"), "REPORT_SEMANTIC")
            add("report.high_delta.primary_stream.frame_seq_events", len(metrics.get("events") or []) == int(high_delta_report_exp.get("occurrence_count") or 0) and all(e.get("previous_frame_number") is not None and e.get("current_frame_number") is not None and e.get("previous_sequence") is not None and e.get("current_sequence") is not None for e in metrics.get("events") or []), metrics.get("events"), "all aggregated events retain Frame/Seq evidence", "REPORT_SEMANTIC")

    report_context = report.get("analysis_context") or {}
    bundle_context = bundle.get("analysis_context") or {}
    add("report.analysis_context_consistency", report_context == bundle_context, report_context, bundle_context, "REPORT")
    report_call = report.get("display_call") or report.get("call")
    add("report.call_consistency", report_call == bundle.get("display_call"), report_call, bundle.get("display_call"), "REPORT")

    return checks
